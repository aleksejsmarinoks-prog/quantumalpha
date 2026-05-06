"""
QA Trade Trigger — Early Event Capture Module
==============================================

First production realization of QuantumAlpha design specs:
  - Anti-Bias Gate (filters.anti_bias_check)
  - Cross-source corroboration gate (filters.corroboration_gate)
  - Signal #45 reborn (Diplomatic Feed → Trade Trigger pipeline)

Public API:
    from bot.trade_trigger import (
        NewsEvent, AssetTrigger, TradeSignal, ClassificationResult,
        Direction, Tier, TriggerVerdict,
        TradeTriggerClassifier, HeuristicScorer, ClaudeClassifier,
        get_triggers_for_event, list_supported_events,
    )

Architecture:
    NewsEvent → Classifier (L1+L2) → Mapping → Filters → Bot push
                                                    ↓
                                          SQLite audit log

Author: QuantumAlpha
Version: 0.1.0
"""

from .models import (
    NewsEvent,
    AssetTrigger,
    TradeSignal,
    ClassificationResult,
    Direction,
    Tier,
    TriggerVerdict,
)

from .classifier import (
    TradeTriggerClassifier,
    HeuristicScorer,
    ClaudeClassifier,
    ClaudeClassifierConfig,
    L1_THRESHOLD,
    L2_THRESHOLD,
)

from .trade_trigger_mapping import (
    get_triggers_for_event,
    list_supported_events,
    is_event_supported,
    get_max_half_life,
    EXCLUDED_TICKERS,
    EVENT_ASSET_MAPPING,
)

from .pipeline import (
    PipelineOrchestrator,
    PipelineConfig,
    PipelineDecision,
)

from .alerts import (
    format_alert,
    format_audit,
    format_sources,
    format_recent,
    build_alert_keyboard,
)

__version__ = "0.3.5"

__all__ = [
    "NewsEvent", "AssetTrigger", "TradeSignal", "ClassificationResult",
    "Direction", "Tier", "TriggerVerdict",
    "TradeTriggerClassifier", "HeuristicScorer", "ClaudeClassifier",
    "ClaudeClassifierConfig", "L1_THRESHOLD", "L2_THRESHOLD",
    "get_triggers_for_event", "list_supported_events",
    "is_event_supported", "get_max_half_life",
    "EXCLUDED_TICKERS", "EVENT_ASSET_MAPPING",
    "PipelineOrchestrator", "PipelineConfig", "PipelineDecision",
    "format_alert", "format_audit", "format_sources", "format_recent",
    "build_alert_keyboard",
]
