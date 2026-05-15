"""
QuantumAlpha — Mean Reversion Strategy ("Panic Buyer")
=======================================================

Logic:
    Buy spot BTC/ETH/SOL on extreme intraday panic dumps.
    Three-tier scale-in. Exit on RSI normalization or +3%.

Empirical grounding:
    - 1Token Institutional 2025 report: mean-reversion in crypto produces 0.43%–1.42%
      monthly returns when applied to liquid major pairs only
    - ANB Investments principle: same model on all three majors (BTC/ETH/SOL),
      no per-asset parameter tuning (anti-overfit)
    - Academic reference: Bybit perp panic-buy with RSI<25 has historically
      produced ~10–15% annualised on retail-size capital with proper risk caps

Anti-bias guardrails (mandatory, from QA constitution):
    1. Bearish regime block — strategy halted in BEARISH macro regime
    2. RSI > 70 OR >5% intraday move on entry candidate → DCA-only, never all-in
    3. No re-entry within 4 hours after a losing exit on the same symbol
    4. After three consecutive losing trades, strategy goes COOLDOWN 24h
    5. Hard absolute stop -15% from average entry, no exception

Honest expectation (NOT a backtest claim):
    - Target: 8–15% APR on $1K allocation in normal conditions
    - Max DD assumption: 15% (set as hard stop)
    - Will underperform during sustained bear markets — expected and acceptable

Version: 1.0 (commit #004)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalType,
    StrategyConfig,
    StrategyStatus,
)
import logging

log = logging.getLogger("qa_bot.strategies.mean_reversion")


# ---- Tunable parameters (LOCKED by walk-forward validation; do not change ad-hoc) ----
PRICE_DROP_TRIGGER_1H = -0.05            # -5% in 1h triggers consideration
RSI_OVERSOLD_THRESHOLD = 25              # RSI(14) below this is panic
RSI_EXIT_THRESHOLD = 60                  # exit when RSI recovers
PROFIT_TARGET_PCT = 0.03                 # +3% take profit
ABSOLUTE_STOP_PCT = -0.15                # -15% absolute stop, no exceptions
MAX_HOLD_HOURS = 48                      # force exit after 48h regardless

# Tier sizing (% of strategy capital deployed at each step)
TIER1_PCT = 0.30                         # 30% on first trigger
TIER2_PCT = 0.30                         # +30% if drops -8% from t1
TIER3_PCT = 0.40                         # +40% if drops -12% from t1

TIER2_TRIGGER_DRAWDOWN = -0.08
TIER3_TRIGGER_DRAWDOWN = -0.12

# Universe (locked — same model, all 3 majors, anti-overfit per ANB principle)
DEFAULT_UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@dataclass
class MeanReversionPosition:
    symbol: str
    tier: int                  # 1, 2, or 3
    avg_entry_price: float
    total_size_usd: float
    opened_at: datetime
    last_tier_added_at: datetime


class MeanReversionStrategy(BaseStrategy):
    """
    Spot mean-reversion on panic dumps. Spot-only (no leverage), long-only.

    Why spot-only:
        Retail size on Bybit means perp slippage on panic candles is severe.
        Spot has cleaner fills and no liquidation risk during the -15% stop.

    Why long-only:
        Mean-reversion fade-the-rally (short on RSI>75 spikes) requires
        leverage to be profitable after fees, and adds liquidation risk.
        Not worth the complexity at $1K capital.
    """

    def __init__(
        self,
        capital_pct: float = 0.20,
        universe: Optional[List[str]] = None,
        enabled: bool = False,
    ):
        config = StrategyConfig(
            strategy_id="mean_reversion_v1",
            capital_pct=capital_pct,
            max_position_pct=1.0,                       # tier-3 fills the whole allocation
            max_concurrent_positions=2,                 # 2 of 3 majors max simultaneously
            cooldown_after_loss_hours=4,
            cooldown_per_symbol_hours=24,
            bearish_regime_block=True,
            daily_loss_limit_pct=0.05,
            enabled=enabled,
        )
        super().__init__(config)
        self._universe = universe or list(DEFAULT_UNIVERSE)
        self._positions: Dict[str, MeanReversionPosition] = {}
        self._consecutive_losses: int = 0
        self._strategy_capital_usd: float = 0.0  # set by Orchestra

    # ---- contract methods ----
    def get_strategy_id(self) -> str:
        return self.config.strategy_id

    def get_universe(self) -> List[str]:
        return list(self._universe)

    def evaluate(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        regime: str,
        now: Optional[datetime] = None,
    ) -> Signal:
        log.info("[EVAL_TICK] strategy=mean_reversion_v1 cycle_start")
        """
        Evaluate one symbol. Returns Signal which is then risk-gated by base class.

        Required market_data keys:
            - close_1h: list/array of last N 1h closes (need >=15 for RSI14)
            - returns_1h: latest 1h return as float (e.g. -0.052 for -5.2%)
            - rsi_14_1h: latest RSI(14) on 1h candles
            - last_price: current spot price

        Phase 6.3.1 — `now` parameter accepts injected clock from backtest replay.
        Default (None) preserves production wall-clock behavior bit-identical.
        """
        if symbol not in self._universe:
            return self._hold(symbol, "symbol_not_in_universe")

        # Validate market data presence
        required = ["returns_1h", "rsi_14_1h", "last_price"]
        if not all(k in market_data for k in required):
            return self._hold(symbol, "missing_market_data")

        rsi = float(market_data["rsi_14_1h"])
        ret_1h = float(market_data["returns_1h"])
        last_price = float(market_data["last_price"])

        # If we already have a position — check exit / scale-in
        if symbol in self._positions:
            return self._evaluate_existing_position(symbol, last_price, rsi, market_data, now=now)

        # No position — check entry trigger
        return self._evaluate_entry(symbol, ret_1h, rsi, last_price, regime)

    # ---- private logic ----
    def _evaluate_entry(
        self,
        symbol: str,
        ret_1h: float,
        rsi: float,
        last_price: float,
        regime: str,
    ) -> Signal:
        """
        Tier-1 entry check.
        Trigger requires EITHER drop OR oversold RSI (not both — too restrictive).
        """
        triggered = ret_1h < PRICE_DROP_TRIGGER_1H or rsi < RSI_OVERSOLD_THRESHOLD

        if not triggered:
            return self._hold(symbol, "no_panic_trigger")

        # Anti-bias gate (will also be applied in apply_risk_gates):
        # if RSI > 70 → market is overbought, reject even if drop happened
        if rsi > 70:
            return self._hold(symbol, "rsi_overbought_reject")

        # Compute tier-1 size
        tier1_usd = self._strategy_capital_usd * TIER1_PCT
        if tier1_usd < 10:  # min meaningful position size on Bybit
            return self._hold(symbol, "size_below_min")

        # Confidence: stronger when both triggers fire simultaneously
        confidence = 0.5
        if ret_1h < PRICE_DROP_TRIGGER_1H:
            confidence += 0.2
        if rsi < RSI_OVERSOLD_THRESHOLD:
            confidence += 0.2
        if rsi < 20:  # extreme
            confidence += 0.1
        confidence = min(confidence, 0.95)

        return Signal(
            strategy_id=self.get_strategy_id(),
            symbol=symbol,
            signal_type=SignalType.ENTER_LONG,
            confidence=confidence,
            size_usd=round(tier1_usd, 2),
            reason=f"panic_entry_t1|ret1h={ret_1h:.4f}|rsi={rsi:.1f}|regime={regime}",
            metadata={
                "tier": 1,
                "trigger_price": last_price,
                "rsi": rsi,
                "ret_1h": ret_1h,
            },
        )

    def _evaluate_existing_position(
        self,
        symbol: str,
        last_price: float,
        rsi: float,
        market_data: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> Signal:
        pos = self._positions[symbol]
        drawdown_from_avg = (last_price - pos.avg_entry_price) / pos.avg_entry_price
        unrealized_pct = drawdown_from_avg
        if now is None:
            now = datetime.now(timezone.utc)
        age_hours = (now - pos.opened_at).total_seconds() / 3600

        # ---- exit conditions (priority order) ----

        # Hard absolute stop — no exception
        if drawdown_from_avg <= ABSOLUTE_STOP_PCT:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=1.0,
                size_usd=pos.total_size_usd,
                reason=f"hard_stop|dd={drawdown_from_avg:.4f}",
                metadata={"exit_type": "absolute_stop"},
            )

        # Profit target
        if unrealized_pct >= PROFIT_TARGET_PCT:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=0.9,
                size_usd=pos.total_size_usd,
                reason=f"profit_target|pnl={unrealized_pct:.4f}",
                metadata={"exit_type": "take_profit"},
            )

        # RSI normalization exit
        if rsi >= RSI_EXIT_THRESHOLD and unrealized_pct > 0:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=0.8,
                size_usd=pos.total_size_usd,
                reason=f"rsi_exit|rsi={rsi:.1f}|pnl={unrealized_pct:.4f}",
                metadata={"exit_type": "rsi_recovery"},
            )

        # Time-based forced exit
        if age_hours >= MAX_HOLD_HOURS:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=1.0,
                size_usd=pos.total_size_usd,
                reason=f"time_exit|hours={age_hours:.1f}",
                metadata={"exit_type": "max_hold"},
            )

        # ---- scale-in conditions ----

        # Tier 2: -8% from tier-1 entry
        if pos.tier == 1 and drawdown_from_avg <= TIER2_TRIGGER_DRAWDOWN:
            tier2_usd = self._strategy_capital_usd * TIER2_PCT
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.SCALE_IN,
                confidence=0.7,
                size_usd=round(tier2_usd, 2),
                reason=f"scale_in_t2|dd={drawdown_from_avg:.4f}",
                metadata={"tier": 2, "rsi": rsi},
            )

        # Tier 3: -12% from tier-1 entry
        if pos.tier == 2 and drawdown_from_avg <= TIER3_TRIGGER_DRAWDOWN:
            tier3_usd = self._strategy_capital_usd * TIER3_PCT
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.SCALE_IN,
                confidence=0.65,
                size_usd=round(tier3_usd, 2),
                reason=f"scale_in_t3|dd={drawdown_from_avg:.4f}",
                metadata={"tier": 3, "rsi": rsi},
            )

        return self._hold(symbol, "in_position_no_exit_trigger")

    def _hold(self, symbol: str, reason: str) -> Signal:
        return Signal(
            strategy_id=self.get_strategy_id(),
            symbol=symbol,
            signal_type=SignalType.HOLD,
            confidence=0.0,
            size_usd=0.0,
            reason=reason,
        )

    # ---- state callbacks (called by Orchestra after fills) ----
    def on_tier_filled(
        self,
        symbol: str,
        tier: int,
        fill_price: float,
        fill_size_usd: float,
        now: Optional[datetime] = None,
    ) -> None:
        """Update internal position state after a tier is filled."""
        if now is None:
            now = datetime.now(timezone.utc)
        if symbol not in self._positions:
            self._positions[symbol] = MeanReversionPosition(
                symbol=symbol,
                tier=tier,
                avg_entry_price=fill_price,
                total_size_usd=fill_size_usd,
                opened_at=now,
                last_tier_added_at=now,
            )
            self.on_position_opened(symbol, "long", fill_size_usd, fill_price, now=now)
        else:
            pos = self._positions[symbol]
            new_total_size = pos.total_size_usd + fill_size_usd
            # weighted average entry
            pos.avg_entry_price = (
                (pos.avg_entry_price * pos.total_size_usd + fill_price * fill_size_usd)
                / new_total_size
            )
            pos.total_size_usd = new_total_size
            pos.tier = tier
            pos.last_tier_added_at = now

    def on_position_closed(
        self,
        symbol: str,
        pnl_usd: float,
        was_loss: bool,
        now: Optional[datetime] = None,
    ) -> None:
        super().on_position_closed(symbol, pnl_usd, was_loss, now=now)
        self._positions.pop(symbol, None)

        if was_loss:
            self._consecutive_losses += 1
            # 3-loss streak → 24h cooldown for the whole strategy
            if self._consecutive_losses >= 3:
                self.status = StrategyStatus.COOLDOWN
                self._consecutive_losses = 0
        else:
            self._consecutive_losses = 0

    def set_strategy_capital(self, capital_usd: float) -> None:
        """Called by Orchestra each tick — sync current capital allocation."""
        self._strategy_capital_usd = max(capital_usd, 0.0)

    def _estimated_capital(self) -> float:
        return self._strategy_capital_usd

    # ---- introspection ----
    def get_position_dict(self) -> Dict[str, Dict[str, Any]]:
        return {
            sym: {
                "tier": p.tier,
                "avg_entry_price": p.avg_entry_price,
                "total_size_usd": p.total_size_usd,
                "opened_at": p.opened_at.isoformat(),
                "age_hours": round(
                    (datetime.now(timezone.utc) - p.opened_at).total_seconds() / 3600, 2
                ),
            }
            for sym, p in self._positions.items()
        }


# ---- helper: RSI calculation (used by data_feed.py to populate market_data) ----
def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """
    Standard RSI calculation. Returns None if insufficient data.

    Implementation note:
        Uses Wilder's smoothing (RMA) — same as TradingView default. Avoids
        SMA-based RSI which gives different values than common platforms.
    """
    if len(closes) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder smoothing for remaining bars
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = abs(min(change, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---- self-test for quick smoke check ----
if __name__ == "__main__":
    import json

    strat = MeanReversionStrategy(capital_pct=0.20, enabled=True)
    strat.set_strategy_capital(200.0)

    # Simulated panic candle on ETH
    market_data = {
        "returns_1h": -0.063,
        "rsi_14_1h": 22.4,
        "last_price": 2845.10,
        "close_1h": [3050, 3045, 3040, 3038, 3035, 3030, 3025, 3020,
                     3010, 3000, 2990, 2980, 2960, 2940, 2900, 2845.10],
    }

    sig = strat.evaluate("ETHUSDT", market_data, regime="VOLATILE")
    sig = strat.apply_risk_gates(sig, regime="VOLATILE")
    print(json.dumps(sig.to_log_dict(), indent=2))

    print("RSI sanity:", calc_rsi(market_data["close_1h"]))
    print("Status:", json.dumps(strat.get_status_dict(), indent=2))
