"""
QA Backtest — MeanReversionAdapter (Phase 6.3.1a Step 5a)
============================================================

Concrete `ProductionAdapter` subclass for the production mean_reversion
strategy. First per-strategy adapter — sets the pattern for funding_arb,
liquidity_vortex, and dca_dips adapters in later steps.

Usage
-----
    from bot.backtest import ReplayEngineV2, make_regime_provider
    from bot.backtest.indicators import IndicatorsProvider
    from bot.backtest.adapters.mean_reversion_adapter import MeanReversionAdapter

    # Setup
    indicators = IndicatorsProvider(bars)
    regime_provider = make_regime_provider(bars)
    adapter = MeanReversionAdapter(starting_capital_usd=200.0)

    # Run
    eng = ReplayEngineV2(
        symbol="ETHUSDT",
        initial_equity=200.0,
        regime_provider=regime_provider,
        indicators_provider=indicators.callable_for_engine(),
    )
    result = eng.run(bars, adapter)
    print(result.summary())
    print(adapter.get_stats())

Production-strategy injection
-----------------------------
By default the adapter wraps `MockMeanReversionStrategy` — a self-contained
mock that emulates the production `BaseStrategy` contract (signal_type,
apply_risk_gates, on_tier_filled, on_position_closed). For production
deploy, swap in the real strategy by setting class attribute:

    class RealMeanReversionAdapter(MeanReversionAdapter):
        strategy_class = MeanReversionStrategy   # from bot.strategies.mean_reversion

OR override `_build_strategy()` for non-default constructor.

The mock implements canonical mean-reversion behavior so the adapter +
walk-forward can be tested END-TO-END without depending on the production
strategy stack:
  - RSI < 20: SCALE_IN (or ENTER_LONG if no position) — tier 2/3 deeper entry
  - RSI < 30 and no position: ENTER_LONG (tier 1 initial)
  - RSI > 70: ENTER_SHORT
  - HIGH_VOL regime: apply_risk_gates downgrades to HOLD (mean-rev struggles in vol)

Anti-lookahead
--------------
Adapter reads indicators from `snapshot.indicators` which is populated by
the Step 1 engine via `indicators_provider`. The IndicatorsProvider enforces
strict anti-lookahead (verified by tests). Adapter itself does no lookahead.

Author: QuantumAlpha
Phase: 6.3.1a Step 5a
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from ..production_adapter_base import ProductionAdapter
from ..models import SnapshotContext

logger = logging.getLogger("qa.backtest.adapters.mean_reversion")


# ===========================================================================
# Tunable constants (mean-reversion specifics)
# ===========================================================================

# RSI thresholds for entry decisions
RSI_OVERSOLD_TIER_1 = 30.0   # initial long entry
RSI_OVERSOLD_TIER_2 = 25.0   # scale-in tier 2
RSI_OVERSOLD_TIER_3 = 20.0   # scale-in tier 3
RSI_OVERBOUGHT      = 70.0   # short entry threshold

# Position sizing per tier (as fraction of strategy capital)
TIER_1_SIZE_PCT = 0.10       # 10% of capital on initial entry
TIER_2_SIZE_PCT = 0.15       # 15% added at tier 2
TIER_3_SIZE_PCT = 0.20       # 20% added at tier 3
MAX_TIER        = 3

# Confidence by RSI extremeness
CONFIDENCE_TIER_1 = 0.65
CONFIDENCE_TIER_2 = 0.75
CONFIDENCE_TIER_3 = 0.85

# Stop-loss / take-profit (used by adapter's get_stop_loss_pct / get_take_profit_pct)
STOP_LOSS_PCT   = 0.02        # 2% initial stop
TAKE_PROFIT_PCT = 0.04        # 4% target (2:1 R:R)

# Regimes where mean-reversion is gated out
GATED_REGIMES = {"HIGH_VOL"}


# ===========================================================================
# Mock production strategy (replace with real strategy import in prod)
# ===========================================================================

class MockSignalType(Enum):
    ENTER_LONG  = "enter_long"
    ENTER_SHORT = "enter_short"
    SCALE_IN    = "scale_in"
    REDUCE      = "reduce"
    EXIT        = "exit"
    HOLD        = "hold"


@dataclass
class MockSignal:
    """Mock signal that emulates the production Signal contract."""
    signal_type: MockSignalType
    size_usd: float
    confidence: float
    strategy_id: str = "mean_reversion_v1"
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_actionable(self) -> bool:
        return self.signal_type != MockSignalType.HOLD


class MockMeanReversionStrategy:
    """Self-contained mock of production mean_reversion strategy.

    Emulates the BaseStrategy contract so the adapter + walk-forward can
    be tested without importing the production stack.

    Replace with `from bot.strategies.mean_reversion import MeanReversionStrategy`
    when deploying for real walk-forward against production code.
    """

    def __init__(self, capital_pct: float = 1.0, enabled: bool = True, **kwargs):
        self.capital_pct = capital_pct
        self.enabled = enabled
        self._strategy_capital: float = 1000.0
        self._position_tiers: Dict[str, int] = {}        # symbol → highest tier filled
        # Audit
        self.gates_called: List[datetime] = []
        self.fills_received: List[tuple] = []
        self.closes_received: List[tuple] = []

    def set_strategy_capital(self, capital: float) -> None:
        self._strategy_capital = capital

    def get_strategy_id(self) -> str:
        return "mean_reversion_v1"

    def evaluate(self, symbol: str, market_data: Dict[str, Any], regime: str) -> MockSignal:
        if not self.enabled:
            return MockSignal(MockSignalType.HOLD, 0.0, 0.0,
                              reason="strategy_disabled")

        rsi = market_data.get("rsi_14_1h")
        last_price = market_data.get("last_price", 0.0)
        if rsi is None or last_price <= 0:
            return MockSignal(MockSignalType.HOLD, 0.0, 0.0,
                              reason="insufficient_data")

        current_tier = self._position_tiers.get(symbol, 0)

        # Tier 3 deepest oversold
        if rsi <= RSI_OVERSOLD_TIER_3 and current_tier < MAX_TIER:
            return self._build_long_entry(symbol, current_tier, rsi)
        # Tier 2
        if rsi <= RSI_OVERSOLD_TIER_2 and current_tier < 2:
            return self._build_long_entry(symbol, current_tier, rsi)
        # Tier 1 initial entry
        if rsi <= RSI_OVERSOLD_TIER_1 and current_tier == 0:
            return self._build_long_entry(symbol, 0, rsi)
        # Short on overbought (single-tier, simple)
        if rsi >= RSI_OVERBOUGHT and current_tier == 0:
            return MockSignal(
                signal_type=MockSignalType.ENTER_SHORT,
                size_usd=self._strategy_capital * TIER_1_SIZE_PCT,
                confidence=CONFIDENCE_TIER_1,
                reason=f"rsi_overbought_short rsi={rsi:.1f}",
                metadata={"tier": 1, "rsi": rsi},
            )

        return MockSignal(MockSignalType.HOLD, 0.0, 0.0,
                          reason=f"rsi_neutral rsi={rsi:.1f}")

    def _build_long_entry(self, symbol: str, current_tier: int, rsi: float) -> MockSignal:
        next_tier = current_tier + 1
        if next_tier == 1:
            size_pct, conf = TIER_1_SIZE_PCT, CONFIDENCE_TIER_1
            stype = MockSignalType.ENTER_LONG
        elif next_tier == 2:
            size_pct, conf = TIER_2_SIZE_PCT, CONFIDENCE_TIER_2
            stype = MockSignalType.SCALE_IN
        else:                                # next_tier == 3
            size_pct, conf = TIER_3_SIZE_PCT, CONFIDENCE_TIER_3
            stype = MockSignalType.SCALE_IN

        return MockSignal(
            signal_type=stype,
            size_usd=self._strategy_capital * size_pct,
            confidence=conf,
            reason=f"rsi_oversold tier={next_tier} rsi={rsi:.1f}",
            metadata={"tier": next_tier, "rsi": rsi},
        )

    def apply_risk_gates(self, signal, regime: str, now: Optional[datetime] = None):
        """Gate signals based on regime. HIGH_VOL → downgrade to HOLD."""
        if now is not None:
            self.gates_called.append(now)
        if regime in GATED_REGIMES:
            return MockSignal(MockSignalType.HOLD, 0.0, 0.0,
                              reason=f"regime_gated regime={regime}")
        return signal

    def on_tier_filled(self, symbol: str, tier: int, fill_price: float,
                        fill_size_usd: float, now: Optional[datetime] = None) -> None:
        self._position_tiers[symbol] = tier
        self.fills_received.append((symbol, tier, fill_price, fill_size_usd, now))

    def on_position_closed(self, symbol: str, pnl_usd: float, was_loss: bool,
                            now: Optional[datetime] = None) -> None:
        self._position_tiers.pop(symbol, None)
        self.closes_received.append((symbol, pnl_usd, was_loss, now))


# ===========================================================================
# MeanReversionAdapter
# ===========================================================================

class MeanReversionAdapter(ProductionAdapter):
    """ProductionAdapter for mean_reversion strategy.

    Reads RSI 14 on 1h-resampled bars + returns_1h from snapshot.indicators
    (populated by IndicatorsProvider via Step 1 engine hook). Delegates
    decision logic to production strategy (mock by default).

    Production deploy: subclass with `strategy_class = MeanReversionStrategy`
    pointing to real production module.
    """

    name: str = "mean_reversion"
    strategy_class: Optional[type] = MockMeanReversionStrategy

    def prepare_market_data(self, snapshot: SnapshotContext) -> Optional[Dict[str, Any]]:
        """Convert SnapshotContext → mean_reversion market_data dict.

        Returns None during warmup (RSI not available yet).
        """
        ind = snapshot.indicators
        rsi = ind.get("rsi_14_1h")
        if rsi is None:
            return None    # warmup

        return {
            "last_price": ind.get("last_price", snapshot.spot),
            "rsi_14_1h": rsi,
            "returns_1h": ind.get("returns_1h", 0.0),
            "vix_level": 20.0,    # placeholder until VIX feed wired
        }

    def get_stop_loss_pct(self, prod_signal: Any, market_data: Dict[str, Any]) -> float:
        return STOP_LOSS_PCT

    def get_take_profit_pct(self, prod_signal: Any, market_data: Dict[str, Any]) -> float:
        return TAKE_PROFIT_PCT

    def required_lookback_bars(self) -> int:
        """Need enough 5m bars for RSI 14 on 1h resampled bars.

        15 complete 1h bars = 15 * 12 = 180 5m bars (need 14+1 for RSI seed).
        Round up to 200 for safety margin.
        """
        return 200

    def evaluation_interval(self) -> timedelta:
        """Mean-reversion evaluates every bar in production (no skip)."""
        return timedelta(minutes=5)
