"""
Base strategy adapter — wraps a live strategy for backtest context.

Why needed:
  - Live strategies (mean_reversion, funding_arb) expect MarketStateProvider
    with WebSocket data + ledger + risk_kernel from `bot.core.*`.
  - In backtest, we provide historical snapshots through SnapshotContext.
  - Adapter translates between the two interfaces.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Optional

from ..models import Signal
from ..replay_engine import SnapshotContext


class BaseAdapter(ABC):
    """
    Subclass and implement `evaluate`. The adapter pattern keeps strategy
    logic in live code while letting backtest call it via a uniform
    interface.
    """

    name: str = "base"

    def reset(self, params: dict) -> None:
        """Reset internal state and apply parameters (override if stateful)."""
        self._params: dict = dict(params)

    @abstractmethod
    def evaluate(self, ctx: SnapshotContext) -> Optional[Signal]:
        """Return a Signal or None."""

    def required_lookback_bars(self) -> int:
        """Number of historical bars required before evaluation starts."""
        return 50                                            # default — most strategies need ≥50 bars

    def evaluation_interval(self) -> timedelta:
        """How often to call evaluate() during replay."""
        return timedelta(minutes=5)                          # default — match mean_rev
