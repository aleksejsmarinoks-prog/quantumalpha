"""
QA Backtester — Data Models
============================

Dataclasses representing trades, fills, signals, and walk-forward window
results. Frozen where reasonable for immutability.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class SignalAction(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"
    HOLD = "HOLD"


# ─────────────────────────────────────────────────────────────────────────────
# Core records
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Signal:
    """Output of a strategy.evaluate() call in backtest context."""
    timestamp: datetime
    symbol: str
    action: SignalAction
    size_usd: float = 0.0
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def is_actionable(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT, SignalAction.EXIT)


@dataclass(frozen=True)
class Fill:
    """Result of a simulated order execution."""
    timestamp: datetime
    symbol: str
    side: Side
    size_usd: float
    fill_price: float
    fee_usd: float
    slippage_bp: float
    order_type: OrderType

    @property
    def notional(self) -> float:
        return self.size_usd


@dataclass
class Trade:
    """Round-trip trade: entry + exit, with realised PnL."""
    symbol: str
    side: Side
    entry_fill: Fill
    exit_fill: Optional[Fill] = None
    funding_pnl_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    is_closed: bool = False

    @property
    def entry_time(self) -> datetime:
        return self.entry_fill.timestamp

    @property
    def exit_time(self) -> Optional[datetime]:
        return self.exit_fill.timestamp if self.exit_fill else None

    @property
    def hold_duration_sec(self) -> int:
        if self.exit_fill is None:
            return 0
        return int((self.exit_fill.timestamp - self.entry_fill.timestamp).total_seconds())

    @property
    def total_fees_usd(self) -> float:
        f = self.entry_fill.fee_usd
        if self.exit_fill is not None:
            f += self.exit_fill.fee_usd
        return f

    def close(self, exit_fill: Fill, funding_pnl_usd: float = 0.0) -> None:
        """Compute realized_pnl and mark closed."""
        self.exit_fill = exit_fill
        self.funding_pnl_usd = funding_pnl_usd
        # Price PnL: long earns when exit > entry, short the inverse
        if self.side == Side.BUY:
            price_pnl_pct = (exit_fill.fill_price - self.entry_fill.fill_price) / self.entry_fill.fill_price
        else:
            price_pnl_pct = (self.entry_fill.fill_price - exit_fill.fill_price) / self.entry_fill.fill_price
        gross = price_pnl_pct * self.entry_fill.size_usd
        self.realized_pnl_usd = gross - self.total_fees_usd + funding_pnl_usd
        self.is_closed = True


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowResult:
    """One walk-forward window: train+test stats + best params."""
    window_idx: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    best_params: dict
    train_metrics: dict
    test_metrics: dict
    train_trades: int
    test_trades: int


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate verdict
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BacktestVerdict:
    """Pass/fail decision against acceptance gates."""
    strategy_name: str
    median_test_sharpe: float
    max_test_mdd_pct: float
    min_test_winrate: float
    pct_profitable_windows: float
    passes_sharpe: bool
    passes_mdd: bool
    passes_winrate: bool
    passes_profitable_pct: bool

    @property
    def passes_all(self) -> bool:
        return all([self.passes_sharpe, self.passes_mdd, self.passes_winrate, self.passes_profitable_pct])

    @property
    def verdict_text(self) -> str:
        return "PASS — ready for live consideration" if self.passes_all else "FAIL — do not deploy to live"
