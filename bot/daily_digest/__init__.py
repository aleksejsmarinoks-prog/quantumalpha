"""
QA Daily Digest — Phase 7.1 (QA Sentinel L1)

Autonomous morning digest of bot activity. Aggregates 24h data and uses
Claude API to produce a Markdown summary for Telegram.

Public API:
    from bot.daily_digest import DailyDigestGenerator, DigestConfig
    from bot.daily_digest.aggregator import (
        aggregate_log_events,
        aggregate_equity_changes,
        aggregate_funding_rates,
        gather_calendar_today,
        gather_bot_health,
        gather_trade_trigger_status,
    )

Scheduler entry point lives in bot/scheduler.py via PHASE7_1 patch.
"""

from .digest_generator import DailyDigestGenerator, DigestConfig
from .aggregator import (
    aggregate_log_events,
    aggregate_equity_changes,
    aggregate_funding_rates,
    gather_calendar_today,
    gather_bot_health,
    gather_trade_trigger_status,
)

__version__ = "7.1.0"

__all__ = [
    "DailyDigestGenerator",
    "DigestConfig",
    "aggregate_log_events",
    "aggregate_equity_changes",
    "aggregate_funding_rates",
    "gather_calendar_today",
    "gather_bot_health",
    "gather_trade_trigger_status",
]
