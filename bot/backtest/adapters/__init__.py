"""Per-strategy backtest adapters."""

# MeanReversion (Step 5a — namespaced re-exports to avoid name clashes with dca_dips)
from .mean_reversion_adapter import (
    MeanReversionAdapter,
    MockMeanReversionStrategy,
)
from . import mean_reversion_adapter as _mr
from . import dca_dips_adapter as _dca

# DcaDips (Step 5c-A)
from .dca_dips_adapter import (
    DcaDipsAdapter,
    MockDcaDipsStrategy,
    SessionTracker,
)

__all__ = [
    # MeanReversion
    "MeanReversionAdapter",
    "MockMeanReversionStrategy",
    # DcaDips
    "DcaDipsAdapter",
    "MockDcaDipsStrategy",
    "SessionTracker",
    # Submodule namespaces (access strategy-specific MockSignal types + constants)
    "mean_reversion_adapter",
    "dca_dips_adapter",
]

# Expose submodules under shorter names too for convenience
mean_reversion_adapter = _mr
dca_dips_adapter = _dca
