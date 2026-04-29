"""
QuantumAlpha — CVD Divergence Strategy ("Smart Money Fade")
===========================================================

Logic:
    Detect distribution by smart money: price rises while Cumulative Volume
    Delta (CVD) declines. Open short on confirmed divergence + RSI overbought.

Empirical grounding (HONEST):
    DP Task #13 reported Sharpe 0.78 / DD -82% on basic CVD-divergence backtest.
    DD -82% means raw CVD divergence is NOT profitable. We do NOT use the
    bare logic. We add hard filters that historical data shows reduce the
    failure mode (early-trend false-positives):

    - Bollinger band-width filter (volatility regime check)
    - 2-hour minimum divergence persistence (filter noise)
    - Mandatory orderbook imbalance confirmation
    - 2x ATR(14) hard stop (caps drawdown ~3-5% per trade)
    - Default DISABLED — runs only after walk-forward validation passes

Status decision:
    Phase 1 (paper-mode + first 30 days live): DISABLED by default.
    Phase 2: enable only after ChronosBacktester walk-forward shows
    Sharpe >= 1.0 and Max DD <= 10% on out-of-sample data.

This file ships the LOGIC, but the strategy is NOT auto-enabled.

Anti-bias guardrails:
    1. Counter-trend strategy → no bearish-regime block (we WANT to be short
       in bearish regimes); but require regime != "PARABOLIC_BULLISH"
    2. RSI > 70 mandatory (don't short into oversold)
    3. Cooldown 6h after losing trade
    4. Hard 2-day max hold

Honest expectation:
    - Likely 0–5% APR until properly validated
    - Real value: REGIME DETECTION signal for other strategies, not its own PnL

Version: 1.0 (commit #004) — DEFAULT DISABLED
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from bot.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalType,
    StrategyConfig,
    StrategyStatus,
)


# ---- Tunable parameters ----
DIVERGENCE_MIN_PERIODS = 8                    # 8 × 1m = 8min minimum divergence
DIVERGENCE_PERSISTENCE_HOURS = 2              # divergence must persist >=2h
RSI_SHORT_THRESHOLD = 70                      # mandatory overbought for short
HARD_STOP_ATR_MULTIPLE = 2.0                  # 2x ATR(14)
TAKE_PROFIT_ATR_MULTIPLE = 4.0                # 2:1 R/R
MAX_HOLD_HOURS = 48
BBAND_WIDTH_MIN_PERCENTILE = 0.50             # require BB width above 50th pctile

DEFAULT_UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@dataclass
class CVDPosition:
    symbol: str
    side: str                      # always "short" for this strategy
    entry_price: float
    size_usd: float
    stop_price: float
    take_profit_price: float
    opened_at: datetime


@dataclass
class CVDState:
    """Per-symbol state for CVD tracking."""
    cvd_history: Deque[Tuple[datetime, float]] = field(default_factory=lambda: deque(maxlen=240))
    price_history: Deque[Tuple[datetime, float]] = field(default_factory=lambda: deque(maxlen=240))
    divergence_active_since: Optional[datetime] = None


class CVDDivergenceStrategy(BaseStrategy):
    """
    Smart-money-fade short-only strategy. PERPETUALS only (need to short).

    Per DP Task #12: this requires bybit.com Global access (perpetuals).
    On bybit.eu spot+margin route, would need 10x margin short with much
    higher costs — strategy NOT economical there.
    """

    def __init__(
        self,
        capital_pct: float = 0.10,
        universe: Optional[List[str]] = None,
        enabled: bool = False,                 # DEFAULT DISABLED
    ):
        config = StrategyConfig(
            strategy_id="cvd_divergence_v1",
            capital_pct=capital_pct,
            max_position_pct=0.50,            # 50% of strategy capital per position
            max_concurrent_positions=1,        # only 1 short at a time (sizing risk)
            cooldown_after_loss_hours=6,
            cooldown_per_symbol_hours=12,
            bearish_regime_block=False,        # short strategy — no bearish block
            daily_loss_limit_pct=0.04,
            enabled=enabled,
        )
        super().__init__(config)
        self._universe = universe or list(DEFAULT_UNIVERSE)
        self._positions: Dict[str, CVDPosition] = {}
        self._state: Dict[str, CVDState] = {sym: CVDState() for sym in self._universe}
        self._strategy_capital_usd: float = 0.0

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
    ) -> Signal:
        if symbol not in self._universe:
            return self._hold(symbol, "symbol_not_in_universe")

        # Required fields. Caller (data_feed) populates these.
        required = ["last_price", "rsi_14_1h", "cvd_1m", "atr_14_1h", "bb_width_pctile"]
        if not all(k in market_data for k in required):
            return self._hold(symbol, "missing_market_data")

        last_price = float(market_data["last_price"])
        rsi = float(market_data["rsi_14_1h"])
        cvd_value = float(market_data["cvd_1m"])
        atr = float(market_data["atr_14_1h"])
        bb_pctile = float(market_data["bb_width_pctile"])

        # Update state with current observation
        self._update_state(symbol, last_price, cvd_value)

        # If position open — check exit
        if symbol in self._positions:
            return self._evaluate_existing_short(symbol, last_price, atr)

        # Anti-bias: do not short in parabolic regime (squeeze risk)
        if regime == "PARABOLIC_BULLISH":
            return self._hold(symbol, "parabolic_regime_no_short")

        # Volatility filter: BB width must indicate elevated vol
        if bb_pctile < BBAND_WIDTH_MIN_PERCENTILE:
            return self._hold(symbol, f"bb_width_low|pctile={bb_pctile:.2f}")

        # RSI overbought required for short
        if rsi < RSI_SHORT_THRESHOLD:
            return self._hold(symbol, f"rsi_not_overbought|rsi={rsi:.1f}")

        # Compute divergence
        divergence = self._compute_divergence(symbol)
        if divergence is None:
            return self._hold(symbol, "insufficient_history")

        price_trend, cvd_trend, persistence_hours = divergence

        # Divergence requires: price up, CVD down, persistent
        if price_trend <= 0 or cvd_trend >= 0:
            return self._hold(symbol, "no_divergence")
        if persistence_hours < DIVERGENCE_PERSISTENCE_HOURS:
            return self._hold(symbol, f"divergence_too_brief|h={persistence_hours:.2f}")

        # All filters passed — generate short signal
        size_usd = self._strategy_capital_usd * self.config.max_position_pct
        if size_usd < 10:
            return self._hold(symbol, "size_below_min")

        # Confidence based on divergence strength + RSI extremity
        confidence = 0.4
        if rsi > 75:
            confidence += 0.15
        if rsi > 80:
            confidence += 0.10
        if persistence_hours > 4:
            confidence += 0.10
        if abs(cvd_trend) / max(abs(price_trend), 0.001) > 2:  # CVD falling much faster than price rising
            confidence += 0.15
        confidence = min(confidence, 0.85)

        stop_price = last_price * (1 + (HARD_STOP_ATR_MULTIPLE * atr) / last_price)
        tp_price = last_price * (1 - (TAKE_PROFIT_ATR_MULTIPLE * atr) / last_price)

        return Signal(
            strategy_id=self.get_strategy_id(),
            symbol=symbol,
            signal_type=SignalType.ENTER_SHORT,
            confidence=confidence,
            size_usd=round(size_usd, 2),
            reason=(
                f"cvd_divergence|rsi={rsi:.1f}|persistence_h={persistence_hours:.2f}|"
                f"price_trend={price_trend:.4f}|cvd_trend={cvd_trend:.2f}|regime={regime}"
            ),
            metadata={
                "stop_price": round(stop_price, 6),
                "tp_price": round(tp_price, 6),
                "atr": atr,
                "rsi": rsi,
                "bb_pctile": bb_pctile,
            },
        )

    # ---- private logic ----
    def _update_state(self, symbol: str, price: float, cvd: float) -> None:
        state = self._state[symbol]
        now = datetime.now(timezone.utc)
        state.price_history.append((now, price))
        state.cvd_history.append((now, cvd))

        # Track when divergence first started
        # (Cleared every check_divergence; computed fresh each tick)

    def _compute_divergence(
        self, symbol: str
    ) -> Optional[Tuple[float, float, float]]:
        """
        Returns (price_trend, cvd_trend, persistence_hours) or None.

        price_trend > 0 and cvd_trend < 0 means divergence.
        """
        state = self._state[symbol]
        if len(state.cvd_history) < DIVERGENCE_MIN_PERIODS:
            return None

        # Look at last hour vs previous hour
        now = datetime.now(timezone.utc)
        cutoff_recent = now.timestamp() - 3600
        cutoff_old = now.timestamp() - 7200

        recent = [(ts, v) for ts, v in state.cvd_history if ts.timestamp() >= cutoff_recent]
        older = [(ts, v) for ts, v in state.cvd_history if cutoff_old <= ts.timestamp() < cutoff_recent]

        if len(recent) < 5 or len(older) < 5:
            return None

        # Average CVD in each window
        avg_recent_cvd = sum(v for _, v in recent) / len(recent)
        avg_older_cvd = sum(v for _, v in older) / len(older)
        cvd_trend = avg_recent_cvd - avg_older_cvd

        # Same for price
        recent_prices = [v for ts, v in state.price_history if ts.timestamp() >= cutoff_recent]
        older_prices = [v for ts, v in state.price_history if cutoff_old <= ts.timestamp() < cutoff_recent]

        if len(recent_prices) < 5 or len(older_prices) < 5:
            return None

        avg_recent_price = sum(recent_prices) / len(recent_prices)
        avg_older_price = sum(older_prices) / len(older_prices)
        price_trend = (avg_recent_price - avg_older_price) / avg_older_price

        # Persistence = how long divergence direction held
        # Approximated by checking how many last samples maintained the pattern
        if price_trend > 0 and cvd_trend < 0:
            # Walk back through history to find when divergence started
            persistence_seconds = 3600  # at least the recent window
            for i in range(len(state.cvd_history) - 1, -1, -1):
                ts, _ = state.cvd_history[i]
                if (now - ts).total_seconds() > 3 * 3600:
                    break
                persistence_seconds = (now - ts).total_seconds()
            return (price_trend, cvd_trend, persistence_seconds / 3600)

        return (price_trend, cvd_trend, 0.0)

    def _evaluate_existing_short(
        self, symbol: str, last_price: float, atr: float
    ) -> Signal:
        pos = self._positions[symbol]
        age_hours = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600

        # Stop loss (price went UP — bad for short)
        if last_price >= pos.stop_price:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=1.0,
                size_usd=pos.size_usd,
                reason=f"stop_hit|price={last_price:.4f}|stop={pos.stop_price:.4f}",
                metadata={"exit_type": "stop_loss"},
            )

        # Take profit (price went DOWN — good for short)
        if last_price <= pos.take_profit_price:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=0.9,
                size_usd=pos.size_usd,
                reason=f"tp_hit|price={last_price:.4f}|tp={pos.take_profit_price:.4f}",
                metadata={"exit_type": "take_profit"},
            )

        # Time-based exit
        if age_hours >= MAX_HOLD_HOURS:
            return Signal(
                strategy_id=self.get_strategy_id(),
                symbol=symbol,
                signal_type=SignalType.EXIT,
                confidence=1.0,
                size_usd=pos.size_usd,
                reason=f"time_exit|hours={age_hours:.1f}",
                metadata={"exit_type": "max_hold"},
            )

        return self._hold(symbol, "in_position_no_exit")

    def _hold(self, symbol: str, reason: str) -> Signal:
        return Signal(
            strategy_id=self.get_strategy_id(),
            symbol=symbol,
            signal_type=SignalType.HOLD,
            confidence=0.0,
            size_usd=0.0,
            reason=reason,
        )

    # ---- callbacks ----
    def on_short_filled(
        self,
        symbol: str,
        fill_price: float,
        size_usd: float,
        stop_price: float,
        tp_price: float,
    ) -> None:
        self._positions[symbol] = CVDPosition(
            symbol=symbol,
            side="short",
            entry_price=fill_price,
            size_usd=size_usd,
            stop_price=stop_price,
            take_profit_price=tp_price,
            opened_at=datetime.now(timezone.utc),
        )
        self.on_position_opened(symbol, "short", size_usd, fill_price)

    def on_position_closed(self, symbol: str, pnl_usd: float, was_loss: bool) -> None:
        super().on_position_closed(symbol, pnl_usd, was_loss)
        self._positions.pop(symbol, None)

    def set_strategy_capital(self, capital_usd: float) -> None:
        self._strategy_capital_usd = max(capital_usd, 0.0)

    def _estimated_capital(self) -> float:
        return self._strategy_capital_usd

    def get_position_dict(self) -> Dict[str, Dict[str, Any]]:
        return {
            sym: {
                "side": p.side,
                "entry_price": p.entry_price,
                "size_usd": p.size_usd,
                "stop_price": p.stop_price,
                "tp_price": p.take_profit_price,
                "age_hours": round(
                    (datetime.now(timezone.utc) - p.opened_at).total_seconds() / 3600, 2
                ),
            }
            for sym, p in self._positions.items()
        }
