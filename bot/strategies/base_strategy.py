"""
QuantumAlpha — Base Strategy Abstract Class
============================================

All trading strategies inherit from BaseStrategy.

Design principles (grounded in ANB Investments + 1Token Institutional Index 2025):
1. Reduce parameters, apply same models to all markets — anti-overfitting
2. Technical price data inputs only (no ML black boxes)
3. Hard risk caps, never exceeded
4. Anti-bias gates before any entry
5. Cooldowns after losing trades
6. Bearish-regime block by default for long strategies

Each strategy must implement:
- evaluate(market_data, regime) -> Signal
- get_capital_allocation() -> float
- get_strategy_id() -> str
- on_position_closed(trade) -> None (for cooldown / state updates)

Reference:
- 1Token / Bybit Institutional 2025 Crypto Quant Strategy Index Report
- ANB Investments Quantitative Delta Neutral methodology
- Pythagoras crypto hedge fund principles

Version: 1.0 (commit #004)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class SignalType(Enum):
    """Unified signal types across all strategies."""
    HOLD = "HOLD"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    SCALE_IN = "SCALE_IN"
    EXIT = "EXIT"
    REDUCE = "REDUCE"


class StrategyStatus(Enum):
    """Operational status of a strategy."""
    DISABLED = "DISABLED"           # not running at all
    PAPER = "PAPER"                  # paper-trading only (no real orders)
    LIVE = "LIVE"                    # live execution allowed
    HALTED = "HALTED"                # killswitch tripped — manual resume needed
    COOLDOWN = "COOLDOWN"            # temporary halt after losing trade


@dataclass
class Signal:
    """
    Unified signal contract returned by all strategies.

    The Orchestra coordinator collects Signals from every strategy and decides
    which (if any) gets capital. Risk Kernel applies final veto before execution.
    """
    strategy_id: str
    symbol: str
    signal_type: SignalType
    confidence: float                # 0.0 to 1.0
    size_usd: float                  # absolute USD notional, 0 for HOLD
    reason: str                      # human-readable why this signal fired
    metadata: Dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_actionable(self) -> bool:
        return self.signal_type != SignalType.HOLD and self.size_usd > 0

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "type": self.signal_type.value,
            "confidence": round(self.confidence, 3),
            "size_usd": round(self.size_usd, 2),
            "reason": self.reason,
            "ts": self.generated_at.isoformat(),
        }


@dataclass
class StrategyConfig:
    """
    Per-strategy configuration. Loaded from .env or per-instance defaults.

    All values are intentionally CONSERVATIVE. Loosen only after walk-forward
    validation in ChronosBacktester confirms safety.
    """
    strategy_id: str
    capital_pct: float                       # fraction of total equity (0.0–1.0)
    max_position_pct: float                  # max single position vs strategy capital
    max_concurrent_positions: int            # across all symbols
    cooldown_after_loss_hours: int = 4       # block re-entry after losing trade
    cooldown_per_symbol_hours: int = 24      # block re-entry per asset after loss
    bearish_regime_block: bool = True        # hard veto in BEARISH regime
    daily_loss_limit_pct: float = 0.05       # stop strategy if daily DD exceeds
    enabled: bool = False                    # default OFF — explicit opt-in only


class BaseStrategy(ABC):
    """
    Abstract base for all QuantumAlpha trading strategies.

    Subclasses provide the ALPHA. This base provides:
    - Risk gating skeleton (cooldowns, bearish-block, daily-loss limits)
    - State tracking (active positions, recent trades)
    - Standardised logging hooks
    - Anti-bias guardrails

    Subclasses MUST NOT bypass apply_risk_gates(). Any signal that fails
    a gate is downgraded to HOLD before reaching Orchestra/Risk Kernel.
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.status = StrategyStatus.PAPER if config.enabled else StrategyStatus.DISABLED

        # cooldown tracking
        self._cooldown_until: Optional[datetime] = None         # global cooldown
        self._symbol_cooldown_until: Dict[str, datetime] = {}   # per-symbol

        # state
        self._daily_pnl_usd: float = 0.0
        self._daily_pnl_reset_date: Optional[datetime] = None
        self._active_positions: Dict[str, Dict[str, Any]] = {}  # symbol -> position dict
        self._signals_emitted: int = 0
        self._signals_filtered_by_gates: int = 0

    # ---- contract methods (must implement) ----
    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        regime: str,
    ) -> Signal:
        """
        Compute a signal for one symbol given current market conditions.

        Args:
            symbol: e.g. "ETHUSDT"
            market_data: dict containing OHLCV, RSI, volume, orderbook depth, etc.
            regime: macro regime classification from QA pipeline:
                    "BULLISH" | "NEUTRAL" | "BEARISH" | "VOLATILE" | "STAGFLATION"

        Returns:
            Signal with HOLD if no actionable opportunity.
        """
        ...

    @abstractmethod
    def get_strategy_id(self) -> str:
        ...

    @abstractmethod
    def get_universe(self) -> List[str]:
        """Symbols this strategy considers."""
        ...

    # ---- shared risk gates ----
    def apply_risk_gates(
        self,
        signal: Signal,
        regime: str,
        now: Optional[datetime] = None,
    ) -> Signal:
        """
        Final risk gate. Downgrades unsafe signals to HOLD.
        Returns the (possibly downgraded) signal.

        Phase 6.3.1 — `now` parameter accepts injected clock from backtest replay.
        Default (None) preserves production wall-clock behavior bit-identical.
        """
        if not signal.is_actionable():
            return signal

        # Gate 1: strategy disabled or halted
        if self.status in (StrategyStatus.DISABLED, StrategyStatus.HALTED):
            return self._downgrade(signal, f"strategy_status_{self.status.value}")

        # Gate 2: bearish regime block (long strategies)
        if (
            self.config.bearish_regime_block
            and regime == "BEARISH"
            and signal.signal_type == SignalType.ENTER_LONG
        ):
            return self._downgrade(signal, "bearish_regime_block")

        # Gate 3: daily loss limit
        self._reset_daily_pnl_if_new_day(now=now)
        capital = self._estimated_capital()
        if (
            capital > 0
            and self._daily_pnl_usd <= -capital * self.config.daily_loss_limit_pct
        ):
            return self._downgrade(signal, "daily_loss_limit_hit")

        # Gate 4: global cooldown
        if now is None:
            now = datetime.now(timezone.utc)
        if self._cooldown_until and now < self._cooldown_until:
            return self._downgrade(signal, "global_cooldown")

        # Gate 5: per-symbol cooldown
        sym_cd = self._symbol_cooldown_until.get(signal.symbol)
        if sym_cd and now < sym_cd:
            return self._downgrade(signal, "symbol_cooldown")

        # Gate 6: max concurrent positions
        if (
            signal.signal_type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT)
            and len(self._active_positions) >= self.config.max_concurrent_positions
            and signal.symbol not in self._active_positions
        ):
            return self._downgrade(signal, "max_concurrent_positions")

        return signal

    def _downgrade(self, signal: Signal, reason: str) -> Signal:
        self._signals_filtered_by_gates += 1
        return Signal(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            signal_type=SignalType.HOLD,
            confidence=0.0,
            size_usd=0.0,
            reason=f"GATED:{reason}|orig={signal.reason}",
            metadata={"original_signal": signal.to_log_dict()},
        )

    # ---- state management ----
    def on_position_opened(
        self,
        symbol: str,
        side: str,
        size_usd: float,
        entry_price: float,
        now: Optional[datetime] = None,
    ) -> None:
        if now is None:
            now = datetime.now(timezone.utc)
        self._active_positions[symbol] = {
            "side": side,
            "size_usd": size_usd,
            "entry_price": entry_price,
            "opened_at": now,
            "tier": 1,
        }

    def on_position_closed(
        self,
        symbol: str,
        pnl_usd: float,
        was_loss: bool,
        now: Optional[datetime] = None,
    ) -> None:
        """Update cooldowns and pnl tracking after a position closes."""
        self._reset_daily_pnl_if_new_day(now=now)
        self._daily_pnl_usd += pnl_usd
        self._active_positions.pop(symbol, None)

        if was_loss:
            if now is None:
                now = datetime.now(timezone.utc)
            self._cooldown_until = now + timedelta(hours=self.config.cooldown_after_loss_hours)
            self._symbol_cooldown_until[symbol] = now + timedelta(
                hours=self.config.cooldown_per_symbol_hours
            )

    def _reset_daily_pnl_if_new_day(self, now: Optional[datetime] = None) -> None:
        if now is None:
            now = datetime.now(timezone.utc)
        if (
            self._daily_pnl_reset_date is None
            or now.date() != self._daily_pnl_reset_date.date()
        ):
            self._daily_pnl_usd = 0.0
            self._daily_pnl_reset_date = now

    def _estimated_capital(self) -> float:
        """Override to return current strategy capital. Default 0 disables limit."""
        return 0.0

    # ---- introspection ----
    def get_status_dict(self) -> Dict[str, Any]:
        self._reset_daily_pnl_if_new_day()
        return {
            "strategy_id": self.config.strategy_id,
            "status": self.status.value,
            "active_positions": len(self._active_positions),
            "daily_pnl_usd": round(self._daily_pnl_usd, 2),
            "signals_emitted": self._signals_emitted,
            "signals_gated": self._signals_filtered_by_gates,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "capital_pct": self.config.capital_pct,
            "enabled": self.config.enabled,
        }

    def set_status(self, new_status: StrategyStatus) -> None:
        self.status = new_status

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} id={self.config.strategy_id} "
            f"status={self.status.value} positions={len(self._active_positions)}>"
        )
