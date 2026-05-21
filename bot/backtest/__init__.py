"""QA Backtest — Phase 6.3.1a (Steps 1 + 2 + 4 + 5a + 5c-A)."""

from .models import (
    Bar, Fill, Position, TakeProfit, Trade, SnapshotContext,
    OpenAction, ScaleInAction, ReduceAction, CloseAction, Action,
    Side, CloseReason,
)
from .replay_engine_v2 import (
    ReplayEngineV2, BacktestResult, AdapterProtocol, RiskKernelProtocol,
)
from .regime_detector import (
    RegimeDetector, RegimeConfig, make_regime_provider, make_trend_regime_provider,
    REGIME_LOW, REGIME_NORMAL, REGIME_HIGH,
    TREND_BULLISH, TREND_NEUTRAL, TREND_BEARISH,
)
from .backtest_risk_kernel import (
    BacktestRiskKernel, TradeRequest, TradeApproval, TradeDecision,
    HaltReason, BacktestClock, WallClock,
    MAX_POSITION_PCT_OF_EQUITY, MAX_TOTAL_LEVERAGE,
    MAX_RISK_PER_TRADE_PCT, DEFAULT_RISK_PER_TRADE_PCT,
    DAILY_DD_LIMIT_PCT, WEEKLY_DD_LIMIT_PCT, TOTAL_DD_LIMIT_PCT,
    CONSECUTIVE_LOSS_COOLDOWN_TRIGGER, MIN_ORDER_USD,
)
from .production_adapter_base import (
    ProductionAdapter,
    PROD_ENTER_LONG, PROD_ENTER_SHORT, PROD_EXIT, PROD_HOLD,
    PROD_SCALE_IN, PROD_REDUCE,
)
from .indicators import (
    IndicatorsProvider, IndicatorsConfig,
)
from .walk_forward import (
    WalkForwardConfig, WalkForwardWindow, WalkForwardReport, WalkForwardHarness,
)
from .load_bars import (
    load_bars_from_cache, split_by_gap,
)

__version__ = "6.3.1b.B"

__all__ = [
    # Models (Step 1)
    "Bar", "Fill", "Position", "TakeProfit", "Trade", "SnapshotContext",
    "OpenAction", "ScaleInAction", "ReduceAction", "CloseAction", "Action",
    "Side", "CloseReason",
    # Engine (Step 1)
    "ReplayEngineV2", "BacktestResult", "AdapterProtocol", "RiskKernelProtocol",
    # Regime (Step 2 + Phase 6.3.1b-B Q6.4)
    "RegimeDetector", "RegimeConfig", "make_regime_provider", "make_trend_regime_provider",
    "REGIME_LOW", "REGIME_NORMAL", "REGIME_HIGH",
    "TREND_BULLISH", "TREND_NEUTRAL", "TREND_BEARISH",
    # Risk kernel (Step 4)
    "BacktestRiskKernel", "TradeRequest", "TradeApproval", "TradeDecision",
    "HaltReason", "BacktestClock", "WallClock",
    "MAX_POSITION_PCT_OF_EQUITY", "MAX_TOTAL_LEVERAGE",
    "MAX_RISK_PER_TRADE_PCT", "DEFAULT_RISK_PER_TRADE_PCT",
    "DAILY_DD_LIMIT_PCT", "WEEKLY_DD_LIMIT_PCT", "TOTAL_DD_LIMIT_PCT",
    "CONSECUTIVE_LOSS_COOLDOWN_TRIGGER", "MIN_ORDER_USD",
    # Production adapter base (Step 4)
    "ProductionAdapter",
    "PROD_ENTER_LONG", "PROD_ENTER_SHORT", "PROD_EXIT", "PROD_HOLD",
    "PROD_SCALE_IN", "PROD_REDUCE",
    # Indicators (Step 5a)
    "IndicatorsProvider", "IndicatorsConfig",
    # Walk-forward (Step 5c-B)
    "WalkForwardConfig", "WalkForwardWindow", "WalkForwardReport", "WalkForwardHarness",
    # Bar loader (Step 6.3.1b-A)
    "load_bars_from_cache", "split_by_gap",
]
