"""QA Daily Digest — Phase 7.2 (Housekeeping & Telemetry Fixes)."""

from .digest_generator import DailyDigestGenerator, DigestConfig
from .aggregator import (
    aggregate_log_events,
    aggregate_equity_changes,
    aggregate_funding_rates,
    gather_calendar_today,
    gather_bot_health,
    gather_trade_trigger_status,
)

__version__ = "7.2.0"

__all__ = [
    "DailyDigestGenerator", "DigestConfig",
    "aggregate_log_events", "aggregate_equity_changes", "aggregate_funding_rates",
    "gather_calendar_today", "gather_bot_health", "gather_trade_trigger_status",
]
