"""
QA Backtest — DcaDipsAdapter (Phase 6.3.1a Step 5c-A)
========================================================

Concrete `ProductionAdapter` subclass for the production dca_dips strategy.
Follows the pattern set by MeanReversionAdapter (Step 5a).

Strategy logic (canonical DCA-on-dips):
  - Tier 1: drawdown ≤ -2% from UTC-session-open → ENTER_LONG
  - Tier 2: drawdown ≤ -5% with tier 1 filled → SCALE_IN
  - Tier 3: drawdown ≤ -10% with tier 2 filled → SCALE_IN
  - Macro event boost: high-importance event within past 2h tightens
    tier 1 trigger to -1% (anticipates event-driven dip)
  - HIGH_VOL regime: apply_risk_gates downgrades to HOLD

Session boundary: UTC midnight. session_open = first 5m bar of UTC day.
session_drawdown = (current_spot - session_open) / session_open.

Anti-lookahead: SessionTracker is fed bars in time order by the adapter
inside `prepare_market_data`. Adapter only reads bars present in snapshot
(which is the current bar). Session open is the open price of the FIRST
seen bar of the current UTC day — not a future bar.

Production strategy injection
-----------------------------
By default wraps `MockDcaDipsStrategy`. For production deploy:

    from bot.strategies.dca_dips import DcaDipsStrategy

    class RealDcaDipsAdapter(DcaDipsAdapter):
        strategy_class = DcaDipsStrategy

Author: QuantumAlpha
Phase: 6.3.1a Step 5c-A
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ..production_adapter_base import ProductionAdapter
from ..models import SnapshotContext

logger = logging.getLogger("qa.backtest.adapters.dca_dips")


# ===========================================================================
# Tunable constants
# ===========================================================================

# Drawdown thresholds for tier triggers (negative = drawdown from session open)
TIER_1_DRAWDOWN_PCT       = -0.02     # -2% → tier 1 ENTER_LONG
TIER_1_DRAWDOWN_PCT_BOOST = -0.01     # -1% if macro-event boost active
TIER_2_DRAWDOWN_PCT       = -0.05     # -5% → tier 2 SCALE_IN
TIER_3_DRAWDOWN_PCT       = -0.10     # -10% → tier 3 SCALE_IN
MAX_TIER                  = 3

# Position sizing per tier (fraction of strategy capital)
TIER_1_SIZE_PCT = 0.15
TIER_2_SIZE_PCT = 0.25
TIER_3_SIZE_PCT = 0.40
# Total potential: 80% of strategy capital. Reserve 20% for emergencies.

# Confidence by tier
CONFIDENCE_TIER_1 = 0.60
CONFIDENCE_TIER_2 = 0.75
CONFIDENCE_TIER_3 = 0.85

# Stop-loss / take-profit per tier (DCA needs deeper stops than mean_rev)
STOP_LOSS_PCT   = 0.05    # 5% stop (DCA accepts deeper drawdown)
TAKE_PROFIT_PCT = 0.06    # 6% target (modest, mean-reversion expected)

# Macro event window
MACRO_BOOST_WINDOW = timedelta(hours=2)   # high-importance event within 2h boosts entry
MACRO_HIGH_IMPORTANCE_VALUES = {"high", "critical", "tier1"}

# Regimes that gate dca_dips
GATED_REGIMES = {"HIGH_VOL"}


# ===========================================================================
# Session tracking helper
# ===========================================================================

class SessionTracker:
    """Tracks session-open price per UTC day per symbol.

    Session boundary: UTC midnight (00:00:00). On first bar of a new day,
    session_open is set to that bar's `open` price. Subsequent bars within
    the same day query the stored session_open.

    Anti-lookahead: tracker only sees bars passed via observe(). Adapter
    calls observe(snapshot.bar) inside prepare_market_data — the current
    bar is the boundary, not a future bar.
    """

    def __init__(self) -> None:
        # (symbol, utc_date) -> session_open_price
        self._opens: Dict[tuple, float] = {}

    def observe(self, symbol: str, bar) -> None:
        """Register a bar's contribution to the current session."""
        key = (symbol, bar.timestamp.date())
        if key not in self._opens:
            self._opens[key] = bar.open

    def session_open(self, symbol: str, timestamp: datetime) -> Optional[float]:
        """Return the session open for the UTC day of `timestamp`.

        Returns None if no bar has been observed yet for that day."""
        key = (symbol, timestamp.date())
        return self._opens.get(key)

    def drawdown(self, symbol: str, timestamp: datetime, current_price: float) -> Optional[float]:
        """Return current pct change from session open. None if no session_open."""
        s_open = self.session_open(symbol, timestamp)
        if s_open is None or s_open <= 0:
            return None
        return (current_price - s_open) / s_open

    def reset(self) -> None:
        self._opens.clear()


# ===========================================================================
# Mock production strategy
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
    signal_type: MockSignalType
    size_usd: float
    confidence: float
    strategy_id: str = "dca_dips_v1"
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_actionable(self) -> bool:
        return self.signal_type != MockSignalType.HOLD


class MockDcaDipsStrategy:
    """Self-contained mock emulating production dca_dips strategy.

    Replace with `from bot.strategies.dca_dips import DcaDipsStrategy`
    via subclass `strategy_class` override for production walk-forward.

    Required market_data keys (provided by DcaDipsAdapter.prepare_market_data):
      - last_price                 current spot
      - session_drawdown_pct       drawdown from session open (None if undefined)
      - has_high_importance_macro  bool, macro event in past 2h window
      - vix_level                  optional VIX (placeholder 20 by default)
    """

    def __init__(self, capital_pct: float = 1.0, enabled: bool = True, **kwargs):
        self.capital_pct = capital_pct
        self.enabled = enabled
        self._strategy_capital: float = 1000.0
        self._position_tiers: Dict[str, int] = {}
        self.gates_called: List[datetime] = []
        self.fills_received: List[tuple] = []
        self.closes_received: List[tuple] = []

    def set_strategy_capital(self, capital: float) -> None:
        self._strategy_capital = capital

    def get_strategy_id(self) -> str:
        return "dca_dips_v1"

    def evaluate(self, symbol: str, market_data: Dict[str, Any], regime: str) -> MockSignal:
        if not self.enabled:
            return MockSignal(MockSignalType.HOLD, 0.0, 0.0, reason="strategy_disabled")

        drawdown = market_data.get("session_drawdown_pct")
        if drawdown is None:
            return MockSignal(MockSignalType.HOLD, 0.0, 0.0,
                              reason="no_session_drawdown")

        macro_boost = bool(market_data.get("has_high_importance_macro", False))
        tier_1_threshold = TIER_1_DRAWDOWN_PCT_BOOST if macro_boost else TIER_1_DRAWDOWN_PCT
        current_tier = self._position_tiers.get(symbol, 0)

        # Tier 3 — deepest
        if drawdown <= TIER_3_DRAWDOWN_PCT and current_tier == 2:
            return self._build_signal(
                MockSignalType.SCALE_IN, TIER_3_SIZE_PCT, CONFIDENCE_TIER_3,
                tier=3, drawdown=drawdown, reason=f"dca_tier3 dd={drawdown*100:.2f}%",
            )
        # Tier 2
        if drawdown <= TIER_2_DRAWDOWN_PCT and current_tier == 1:
            return self._build_signal(
                MockSignalType.SCALE_IN, TIER_2_SIZE_PCT, CONFIDENCE_TIER_2,
                tier=2, drawdown=drawdown, reason=f"dca_tier2 dd={drawdown*100:.2f}%",
            )
        # Tier 1 — entry trigger (boost-aware)
        if drawdown <= tier_1_threshold and current_tier == 0:
            return self._build_signal(
                MockSignalType.ENTER_LONG, TIER_1_SIZE_PCT, CONFIDENCE_TIER_1,
                tier=1, drawdown=drawdown,
                reason=(
                    f"dca_tier1 dd={drawdown*100:.2f}% "
                    f"{'macro_boost' if macro_boost else 'normal'}"
                ),
            )

        return MockSignal(MockSignalType.HOLD, 0.0, 0.0,
                          reason=f"dd={drawdown*100:.2f}% tier={current_tier}")

    def _build_signal(self, stype, size_pct, confidence, tier, drawdown, reason):
        return MockSignal(
            signal_type=stype,
            size_usd=self._strategy_capital * size_pct,
            confidence=confidence,
            reason=reason,
            metadata={"tier": tier, "session_drawdown_pct": drawdown},
        )

    def apply_risk_gates(self, signal, regime: str, now: Optional[datetime] = None):
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
# Adapter
# ===========================================================================

class DcaDipsAdapter(ProductionAdapter):
    """ProductionAdapter for dca_dips strategy.

    Tracks session-open prices internally (per symbol per UTC day) and feeds
    drawdown + macro-event-flag into the production strategy via market_data.
    """

    name: str = "dca_dips"
    strategy_class: Optional[type] = MockDcaDipsStrategy

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sessions = SessionTracker()

    def reset(self, params: Optional[dict] = None) -> None:
        """Reset adapter for walk-forward window boundary."""
        super().reset(params)
        # Session tracker reset on new window
        if not hasattr(self, "_sessions"):
            self._sessions = SessionTracker()
        else:
            self._sessions.reset()

    def prepare_market_data(self, snapshot: SnapshotContext) -> Optional[Dict[str, Any]]:
        """Convert SnapshotContext → dca_dips market_data dict.

        Updates session tracker with the current bar's open price, then
        computes drawdown from session open. Pulls high-importance macro
        events from snapshot.macro_events within the past MACRO_BOOST_WINDOW.

        Returns None if session_open not yet established (first bar of session
        with no prior bar — should be rare since the SAME bar registers open).
        Actually it's never None because observe() registers the current bar
        before drawdown is computed.
        """
        # Register current bar in session tracker
        self._sessions.observe(snapshot.symbol, snapshot.bar)

        drawdown = self._sessions.drawdown(
            snapshot.symbol, snapshot.timestamp, snapshot.spot,
        )
        if drawdown is None:
            return None    # extreme edge case

        # Macro events: check if any high-importance event happened in past 2h
        macro_boost = self._has_high_importance_macro(
            snapshot.macro_events, snapshot.timestamp,
        )

        return {
            "last_price": snapshot.spot,
            "session_drawdown_pct": drawdown,
            "has_high_importance_macro": macro_boost,
            "vix_level": 20.0,    # placeholder
        }

    def get_stop_loss_pct(self, prod_signal: Any, market_data: Dict[str, Any]) -> float:
        return STOP_LOSS_PCT

    def get_take_profit_pct(self, prod_signal: Any, market_data: Dict[str, Any]) -> float:
        return TAKE_PROFIT_PCT

    def required_lookback_bars(self) -> int:
        """dca_dips needs only intra-day data (session start to now).
        288 5m bars = 24h. Conservative for warmup.
        """
        return 288

    # ─── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _has_high_importance_macro(
        macro_events: tuple, current_ts: datetime,
    ) -> bool:
        """Return True if any high-importance macro event happened in
        [current_ts - MACRO_BOOST_WINDOW, current_ts]."""
        if not macro_events:
            return False
        boost_start = current_ts - MACRO_BOOST_WINDOW
        for event in macro_events:
            if not isinstance(event, dict):
                continue
            importance = str(event.get("importance", "")).lower()
            if importance not in MACRO_HIGH_IMPORTANCE_VALUES:
                continue
            # Event must have a time field (ISO or datetime)
            event_time = event.get("time_utc") or event.get("timestamp")
            if event_time is None:
                continue
            if isinstance(event_time, str):
                try:
                    et = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                except ValueError:
                    continue
            elif isinstance(event_time, datetime):
                et = event_time
            else:
                continue
            # Make timezone-aware if needed
            if et.tzinfo is None:
                et = et.replace(tzinfo=timezone.utc)
            if boost_start <= et <= current_ts:
                return True
        return False
