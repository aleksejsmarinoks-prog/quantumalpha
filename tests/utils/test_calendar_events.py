"""
Tests for bot.utils.calendar_events.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.utils.calendar_events import (
    CALENDAR_2026,
    CalendarEvent,
    events_in_range,
    in_event_window,
    next_event,
)


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestInEventWindow:
    def test_outside_window_false(self):
        # Far from any 2026 event
        far = _utc(2026, 1, 1, 0, 0)
        assert in_event_window(far, window_min=15) is False

    def test_exactly_at_event_true(self):
        # FOMC 2026-01-28 18:00 UTC
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time) is True

    def test_14_min_before_true(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time - timedelta(minutes=14)) is True

    def test_16_min_before_false(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time - timedelta(minutes=16)) is False

    def test_15_min_before_inclusive_true(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time - timedelta(minutes=15)) is True

    def test_15_min_after_inclusive_true(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time + timedelta(minutes=15)) is True

    def test_16_min_after_false(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time + timedelta(minutes=16)) is False

    def test_naive_datetime_assumed_utc(self):
        # Naive datetime should be coerced to UTC; should still match
        naive = datetime(2026, 1, 28, 18, 0)
        assert in_event_window(naive) is True

    def test_custom_window_size(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        # Within 60-min window
        assert in_event_window(ev_time + timedelta(minutes=30), window_min=60) is True
        # Outside 5-min window
        assert in_event_window(ev_time + timedelta(minutes=10), window_min=5) is False

    def test_empty_calendar_always_false(self):
        ev_time = _utc(2026, 1, 28, 18, 0)
        assert in_event_window(ev_time, events=[]) is False

    def test_default_now_no_crash(self):
        # Just ensure default-now path works
        result = in_event_window()
        assert isinstance(result, bool)


class TestNextEvent:
    def test_next_event_returns_upcoming(self):
        now = _utc(2026, 6, 1, 0, 0)
        nxt = next_event(now)
        assert nxt is not None
        assert nxt.when_utc > now

    def test_next_event_none_after_calendar_end(self):
        far_future = _utc(2099, 1, 1, 0, 0)
        assert next_event(far_future) is None

    def test_next_event_picks_earliest(self):
        # Between Jan FOMC and Feb NFP
        now = _utc(2026, 1, 29, 0, 0)
        nxt = next_event(now)
        assert nxt is not None
        # Feb NFP is 2026-02-06 — closest event after 2026-01-29
        assert nxt.when_utc < _utc(2026, 2, 28, 0, 0)


class TestEventsInRange:
    def test_returns_events_in_range(self):
        start = _utc(2026, 1, 1, 0, 0)
        end = _utc(2026, 2, 1, 0, 0)
        events = events_in_range(start, end)
        assert len(events) >= 2  # at least Jan FOMC + Jan NFP + Jan CPI
        for ev in events:
            assert start <= ev.when_utc <= end

    def test_empty_range_returns_empty(self):
        start = _utc(2099, 1, 1, 0, 0)
        end = _utc(2099, 12, 31, 0, 0)
        assert events_in_range(start, end) == []


class TestCalendarSchedule:
    def test_8_fomc_meetings_in_2026(self):
        fomc = [e for e in CALENDAR_2026 if e.name == "FOMC"]
        assert len(fomc) == 8

    def test_12_nfp_releases_in_2026(self):
        nfp = [e for e in CALENDAR_2026 if e.name == "NFP"]
        assert len(nfp) == 12

    def test_12_cpi_releases_in_2026(self):
        cpi = [e for e in CALENDAR_2026 if e.name == "CPI"]
        assert len(cpi) == 12

    def test_all_events_tz_aware_utc(self):
        for ev in CALENDAR_2026:
            assert ev.when_utc.tzinfo is timezone.utc
