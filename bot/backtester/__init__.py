"""QA Backtester package."""
from .data_loader import BybitDataLoader
from .execution_sim import ExecutionSimulator, MarketSnapshot
from .metrics import compute_metrics
from .models import (
    BacktestVerdict,
    Fill,
    OrderType,
    Side,
    Signal,
    SignalAction,
    Trade,
    WindowResult,
)
from .replay_engine import ReplayEngine, SnapshotContext
from .report import write_backtest_report
from .walk_forward import (
    WalkForwardValidator,
    aggregate_verdict,
    generate_windows,
    grid_combinations,
)

__all__ = [
    "BacktestVerdict",
    "BybitDataLoader",
    "ExecutionSimulator",
    "Fill",
    "MarketSnapshot",
    "OrderType",
    "ReplayEngine",
    "Side",
    "Signal",
    "SignalAction",
    "SnapshotContext",
    "Trade",
    "WalkForwardValidator",
    "WindowResult",
    "aggregate_verdict",
    "compute_metrics",
    "generate_windows",
    "grid_combinations",
    "write_backtest_report",
]
