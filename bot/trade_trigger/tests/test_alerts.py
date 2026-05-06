"""Alert formatting tests. No network, no aiogram bot needed for text."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from bot.trade_trigger.models import (
    TradeSignal, AssetTrigger, Direction, TriggerVerdict,
)
from bot.trade_trigger.alerts import (
    format_alert, format_audit, format_sources, format_recent,
    _wrap_text, _short_time,
)


@pytest.fixture
def sample_signal(utc_now):
    return TradeSignal(
        event_type="hormuz_easing",
        triggers=[
            AssetTrigger(
                ticker="ETH/USDT", venue="Bybit",
                direction=Direction.LONG, conviction=0.75,
                suggested_size_pct_bucket=7.5,
                invalidation_reason="ETH < $2300",
                half_life_minutes=120,
            ),
            AssetTrigger(
                ticker="SOL/USDT", venue="Bybit",
                direction=Direction.LONG, conviction=0.65,
                suggested_size_pct_bucket=6.5,
                half_life_minutes=90,
            ),
            AssetTrigger(
                ticker="SHEL.L", venue="T212",
                direction=Direction.SHORT, conviction=0.45,
                suggested_size_pct_bucket=4.5,
                half_life_minutes=240,
            ),
        ],
        sources=[
            "https://truthsocial.com/...",
            "https://reuters.com/...",
        ],
        first_seen_utc=utc_now,
        actionability_score=7.5,
        reasoning=(
            "Direct authoritative source bypass. "
            "Trump statement on Hormuz transit indicates US-Iran de-escalation. "
            "ETH ETF inflows 9-day streak supports bid."
        ),
        verdict=TriggerVerdict.FIRE,
    )


# ---------------------------------------------------------------------------
# format_alert
# ---------------------------------------------------------------------------

class TestFormatAlert:

    def test_contains_signal_id(self, sample_signal):
        text = format_alert(sample_signal, signal_id=42)
        assert "#42" in text

    def test_contains_event_type(self, sample_signal):
        text = format_alert(sample_signal, signal_id=1)
        assert "hormuz_easing" in text

    def test_contains_score(self, sample_signal):
        text = format_alert(sample_signal, signal_id=1)
        assert "7.5" in text

    def test_contains_all_tickers(self, sample_signal):
        text = format_alert(sample_signal, signal_id=1)
        assert "ETH/USDT" in text
        assert "SOL/USDT" in text
        assert "SHEL.L" in text

    def test_contains_directions(self, sample_signal):
        text = format_alert(sample_signal, signal_id=1)
        assert "LONG" in text
        assert "SHORT" in text

    def test_contains_invalidation(self, sample_signal):
        text = format_alert(sample_signal, signal_id=1)
        assert "ETH < $2300" in text

    def test_contains_reasoning(self, sample_signal):
        text = format_alert(sample_signal, signal_id=1)
        assert "Trump statement" in text

    def test_no_markdown_chars(self, sample_signal):
        """Plain text only — should not have markdown that aiogram parses."""
        text = format_alert(sample_signal, signal_id=1)
        # Asterisks and underscores would be problems with parse_mode=MARKDOWN
        # We use parse_mode=None so technically OK, but defensive check
        # Allow them in numbers like _0.75_ — just verify no obvious **bold**
        assert "**" not in text
        assert "__" not in text

    def test_cyrillic_safe(self):
        """No accidental encoding issues with extended chars."""
        signal = TradeSignal(
            event_type="hormuz_easing",
            triggers=[],
            sources=[],
            first_seen_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
            actionability_score=7.0,
            reasoning="Тест с кириллицей",
        )
        text = format_alert(signal, signal_id=1)
        assert "Тест" in text


# ---------------------------------------------------------------------------
# format_audit
# ---------------------------------------------------------------------------

class TestFormatAudit:

    def test_renders_signal_meta(self):
        signal_row = {
            "id": 7,
            "event_type": "fed_dovish_signal",
            "actionability_score": 8.2,
            "headline": "Warsh signals dovish stance",
            "source_domain": "federalreserve.gov",
            "fired_utc": "2026-05-06T14:00:00",
            "user_action": None,
            "raw_id": "abc123",
        }
        audit = [
            {"filter_name": "velocity", "passed": 1, "reason": "fresh, age 2.0min"},
            {"filter_name": "classifier", "passed": 1, "reason": "L1+L2 score 8.2"},
            {"filter_name": "corroboration", "passed": 1, "reason": "direct source bypass"},
        ]
        text = format_audit(signal_row, audit)
        assert "AUDIT #7" in text
        assert "fed_dovish_signal" in text
        assert "Warsh" in text
        assert "velocity" in text
        assert "✅" in text

    def test_renders_failed_step(self):
        signal_row = {"id": 1, "event_type": "test", "actionability_score": 0}
        audit = [
            {"filter_name": "velocity", "passed": 0, "reason": "stale, age 200min"},
        ]
        text = format_audit(signal_row, audit)
        assert "❌" in text

    def test_no_audit_rows(self):
        signal_row = {"id": 1, "event_type": "test", "actionability_score": 0}
        text = format_audit(signal_row, [])
        assert "No audit log" in text


# ---------------------------------------------------------------------------
# format_sources
# ---------------------------------------------------------------------------

class TestFormatSources:

    def test_empty_state(self):
        text = format_sources([])
        assert "No sources" in text

    def test_healthy_source(self):
        rows = [{
            "source_name": "polymarket",
            "consecutive_fails": 0,
            "total_polls": 120,
            "total_events": 5,
            "last_success_utc": "2026-05-06T08:30:00",
            "last_error": None,
        }]
        text = format_sources(rows)
        assert "polymarket" in text
        assert "🟢" in text
        assert "120" in text

    def test_failing_source(self):
        rows = [{
            "source_name": "broken",
            "consecutive_fails": 3,
            "total_polls": 50,
            "total_events": 0,
            "last_success_utc": None,
            "last_error": "Connection timeout",
        }]
        text = format_sources(rows)
        assert "🔴" in text
        assert "fail x3" in text
        assert "Connection timeout" in text


# ---------------------------------------------------------------------------
# format_recent
# ---------------------------------------------------------------------------

class TestFormatRecent:

    def test_empty(self):
        assert "No signals" in format_recent([])

    def test_renders_rows(self):
        rows = [
            {
                "id": 5, "event_type": "hormuz_easing",
                "actionability_score": 7.5, "user_action": "confirmed",
                "fired_utc": "2026-05-06T08:00:00",
            },
            {
                "id": 4, "event_type": "fed_dovish_signal",
                "actionability_score": 8.0, "user_action": None,
                "fired_utc": "2026-05-06T07:30:00",
            },
        ]
        text = format_recent(rows)
        assert "#5" in text
        assert "#4" in text
        assert "hormuz_easing" in text
        assert "confirmed" in text
        assert "no action" in text


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class TestInternalHelpers:

    def test_wrap_text_short(self):
        result = _wrap_text("short text", width=80)
        assert result == ["short text"]

    def test_wrap_text_long(self):
        text = "word " * 30  # ~150 chars
        result = _wrap_text(text, width=40)
        assert len(result) > 1
        assert all(len(line) <= 50 for line in result)  # some slack for word boundaries

    def test_wrap_text_empty(self):
        result = _wrap_text("", width=80)
        assert result  # not empty list

    def test_short_time_iso(self):
        assert _short_time("2026-05-06T08:30:00") == "05-06 08:30"

    def test_short_time_none(self):
        assert _short_time(None) == "—"
