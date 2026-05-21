"""
QA Backtest — Data Models for ReplayEngine v2 (Phase 6.3.1a Step 1)
======================================================================

Canonical data structures for the rebuilt backtester. Designed to support:
  - Multi-position state (concurrent positions, tier-based scaling)
  - Action dispatch (OPEN / SCALE_IN / REDUCE / CLOSE)
  - SnapshotContext with regime + macro_events (Step 2 will populate)
  - Adapter callbacks (on_fill, on_position_closed)
  - BacktestRiskKernel hook (Step 4 will wire policy)

All datetime values are UTC.

Author: QuantumAlpha
Phase: 6.3.1a Step 1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Tuple


Side = Literal["LONG", "SHORT"]
CloseReason = Literal["adapter", "stop_loss", "take_profit", "end_of_data", "risk_kernel"]


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """OHLCV bar with end-of-bar timestamp. timestamp = bar close time, UTC."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Bar.timestamp must be timezone-aware (UTC)")


# ---------------------------------------------------------------------------
# Take-profit ladder
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TakeProfit:
    """Take-profit level. fraction = portion of CURRENT position to close.
    Example: TakeProfit(price=2050.0, fraction=0.5) closes half on hit.
    """
    price: float
    fraction: float = 1.0   # 1.0 → close all; 0.5 → close half
    triggered: bool = False  # mutable via dataclasses.replace if needed


# ---------------------------------------------------------------------------
# Fill record (one per OPEN or SCALE_IN execution)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    price: float          # executed price (post-slippage)
    qty: float            # always positive; direction implied by Position.side
    commission: float     # commission paid in quote currency

    @property
    def notional(self) -> float:
        return self.price * self.qty


# ---------------------------------------------------------------------------
# Position state (multi-tier)
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Live position. Supports multi-fill accumulation (SCALE_IN) and partial
    closes (REDUCE). `size` is the net open quantity; positions with size=0
    have been fully closed.
    """
    id: str
    symbol: str
    side: Side
    opened_at: datetime
    entries: List[Fill] = field(default_factory=list)
    size: float = 0.0                                # net quantity (always >= 0)
    avg_entry_price: float = 0.0                     # qty-weighted across entries
    realized_pnl: float = 0.0                        # accumulated from REDUCE/CLOSE
    total_commission: float = 0.0                    # all-in commission paid
    stop_loss: Optional[float] = None
    take_profits: List[TakeProfit] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)
    closed_at: Optional[datetime] = None
    close_reason: Optional[CloseReason] = None

    # ----- Mutation helpers (called by engine, not adapter) -----

    def add_fill(self, fill: Fill) -> None:
        """Apply a new fill (OPEN or SCALE_IN). Updates size + avg_entry_price."""
        if self.size == 0:
            self.avg_entry_price = fill.price
        else:
            # Qty-weighted average
            new_total = (self.avg_entry_price * self.size) + (fill.price * fill.qty)
            self.avg_entry_price = new_total / (self.size + fill.qty)
        self.size += fill.qty
        self.entries.append(fill)
        self.total_commission += fill.commission

    def apply_reduction(self, qty: float, exit_price: float, commission: float) -> float:
        """Partially close position. Returns realized PnL for this reduction."""
        if qty <= 0:
            raise ValueError(f"reduction qty must be positive, got {qty}")
        if qty > self.size + 1e-9:
            raise ValueError(
                f"reduction qty {qty} exceeds position size {self.size}"
            )

        # PnL on the reduced chunk
        if self.side == "LONG":
            pnl = (exit_price - self.avg_entry_price) * qty
        else:
            pnl = (self.avg_entry_price - exit_price) * qty

        pnl_net = pnl - commission
        self.realized_pnl += pnl_net
        self.total_commission += commission
        self.size -= qty
        if self.size < 1e-9:
            self.size = 0.0
        return pnl_net

    def is_closed(self) -> bool:
        return self.size == 0.0

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.size == 0:
            return 0.0
        if self.side == "LONG":
            return (mark_price - self.avg_entry_price) * self.size
        return (self.avg_entry_price - mark_price) * self.size


# ---------------------------------------------------------------------------
# Actions (adapter → engine)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenAction:
    """Open a new position. Engine assigns position_id."""
    symbol: str
    side: Side
    qty: float
    stop_loss: Optional[float] = None
    take_profits: Tuple[TakeProfit, ...] = ()
    metadata: Dict[str, object] = field(default_factory=dict)
    # Adapter-supplied label for tracking (engine echoes back in callbacks)
    tag: str = ""


@dataclass(frozen=True)
class ScaleInAction:
    """Add to existing position. Same side as the position."""
    position_id: str
    qty: float
    tag: str = ""
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ReduceAction:
    """Partially close position. qty must be <= current position size."""
    position_id: str
    qty: float
    tag: str = ""


@dataclass(frozen=True)
class CloseAction:
    """Fully close position."""
    position_id: str
    reason: str = "adapter"
    tag: str = ""


Action = object  # Union[OpenAction, ScaleInAction, ReduceAction, CloseAction] — kept loose for adapter flexibility


# ---------------------------------------------------------------------------
# SnapshotContext (engine → adapter, once per bar)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotContext:
    """Everything the adapter needs to make a decision on this bar.

    Phase 6.3.1a Step 1 ships the structure. Step 2 (regime_detector) will
    populate `regime`. Step 4 (BacktestRiskKernel) will read `equity` to
    enforce limits. Macro events are populated by `calendar_provider`
    (optional, supplied to ReplayEngine constructor).
    """
    timestamp: datetime
    symbol: str
    bar: Bar
    spot: float                                                # mark price (usually bar.close)
    equity: float                                              # current equity, for risk checks
    open_position_count: int                                   # quick check without iterating
    regime: Optional[str] = None                               # "LOW_VOL" / "HIGH_VOL" / None
    macro_events: Tuple[Dict[str, object], ...] = ()           # {time_utc, name, importance}
    indicators: Dict[str, float] = field(default_factory=dict) # precomputed (RSI, ATR, …)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("SnapshotContext.timestamp must be tz-aware (UTC)")


# ---------------------------------------------------------------------------
# Trade record (for analysis / reporting)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trade:
    """A closed position aggregated into a single trade record.

    Generated when a position fully closes (size→0). Multiple SCALE_IN /
    REDUCE / CLOSE events on the same position roll up into one Trade.
    """
    position_id: str
    symbol: str
    side: Side
    opened_at: datetime
    closed_at: datetime
    avg_entry_price: float
    avg_exit_price: float
    qty_total: float       # total qty traded (sum of fills)
    realized_pnl: float
    commission: float
    close_reason: CloseReason
    metadata: Dict[str, object] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()

    @property
    def return_pct(self) -> float:
        """Return on entry notional, post-commission."""
        notional = self.avg_entry_price * self.qty_total
        if notional <= 0:
            return 0.0
        return (self.realized_pnl / notional) * 100.0
