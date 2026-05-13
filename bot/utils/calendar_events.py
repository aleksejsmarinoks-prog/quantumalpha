"""
LV1 — Macro Event Calendar (2026)
==================================

Hardcoded schedule of major macro events (FOMC, NFP, CPI) for 2026, used by
the SELF_CRITIQUE Gate factor #12 (in_calendar_event_window).

Times are UTC. NFP is published 12:30 UTC year-round (1st Friday).
CPI dates vary — sourced from BLS schedule. FOMC has 8 meetings/year, all
Wednesdays at 18:00 UTC (post-DST shifts to 19:00 UTC).

For Phase 6.4+ this should be replaced by a Trading Economics API integration
(paid). Until then, schedule is static and reviewed quarterly.

Author: QuantForge / QuantumAlpha
Phase: 6.2
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Event types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CalendarEvent:
    name: str           # "FOMC" | "NFP" | "CPI"
    when_utc: datetime  # tz-aware UTC


# ─────────────────────────────────────────────────────────────────────────────
# Hardcoded 2026 schedule
# ─────────────────────────────────────────────────────────────────────────────

def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


CALENDAR_2026: tuple[CalendarEvent, ...] = (
    # FOMC — 8 meetings/year, Wednesdays 18:00 UTC (Nov/Dec post-DST → 19:00 UTC)
    CalendarEvent("FOMC", _utc(2026, 1, 28, 18, 0)),
    CalendarEvent("FOMC", _utc(2026, 3, 18, 18, 0)),
    CalendarEvent("FOMC", _utc(2026, 5, 6, 18, 0)),
    CalendarEvent("FOMC", _utc(2026, 6, 17, 18, 0)),
    CalendarEvent("FOMC", _utc(2026, 7, 29, 18, 0)),
    CalendarEvent("FOMC", _utc(2026, 9, 16, 18, 0)),
    CalendarEvent("FOMC", _utc(2026, 11, 4, 19, 0)),
    CalendarEvent("FOMC", _utc(2026, 12, 16, 19, 0)),

    # NFP — 1st Friday of month, 13:30 UTC (12:30 ET)
    CalendarEvent("NFP", _utc(2026, 1, 2, 13, 30)),
    CalendarEvent("NFP", _utc(2026, 2, 6, 13, 30)),
    CalendarEvent("NFP", _utc(2026, 3, 6, 13, 30)),
    CalendarEvent("NFP", _utc(2026, 4, 3, 13, 30)),       # post-DST → 12:30 UTC
    CalendarEvent("NFP", _utc(2026, 5, 1, 12, 30)),
    CalendarEvent("NFP", _utc(2026, 6, 5, 12, 30)),
    CalendarEvent("NFP", _utc(2026, 7, 3, 12, 30)),
    CalendarEvent("NFP", _utc(2026, 8, 7, 12, 30)),
    CalendarEvent("NFP", _utc(2026, 9, 4, 12, 30)),
    CalendarEvent("NFP", _utc(2026, 10, 2, 12, 30)),
    CalendarEvent("NFP", _utc(2026, 11, 6, 13, 30)),       # post-DST end
    CalendarEvent("NFP", _utc(2026, 12, 4, 13, 30)),

    # CPI — published ~mid-month, 13:30 UTC (12:30 ET; post-DST 12:30 UTC)
    # Dates per BLS 2026 release schedule (estimated; review quarterly).
    CalendarEvent("CPI", _utc(2026, 1, 14, 13, 30)),
    CalendarEvent("CPI", _utc(2026, 2, 11, 13, 30)),
    CalendarEvent("CPI", _utc(2026, 3, 11, 13, 30)),
    CalendarEvent("CPI", _utc(2026, 4, 14, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 5, 13, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 6, 10, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 7, 14, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 8, 12, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 9, 10, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 10, 14, 12, 30)),
    CalendarEvent("CPI", _utc(2026, 11, 12, 13, 30)),
    CalendarEvent("CPI", _utc(2026, 12, 10, 13, 30)),
)


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def in_event_window(
    now: Optional[datetime] = None,
    window_min: int = 15,
    events: Iterable[CalendarEvent] = CALENDAR_2026,
) -> bool:
    """
    Return True if `now` (defaults to UTC now) is within ±`window_min` minutes
    of any event in `events`.

    Boundary semantics: inclusive on both sides.
        event 12:00, window 15 → covers [11:45, 12:15]
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        # Reject naive datetimes — caller should pass tz-aware
        now = now.replace(tzinfo=timezone.utc)
    delta = timedelta(minutes=window_min)
    for ev in events:
        if abs(now - ev.when_utc) <= delta:
            return True
    return False


def next_event(
    now: Optional[datetime] = None,
    events: Iterable[CalendarEvent] = CALENDAR_2026,
) -> Optional[CalendarEvent]:
    """Return the next upcoming event, or None if all events are past."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    upcoming = [ev for ev in events if ev.when_utc >= now]
    if not upcoming:
        return None
    return min(upcoming, key=lambda e: e.when_utc)


def events_in_range(
    start: datetime,
    end: datetime,
    events: Iterable[CalendarEvent] = CALENDAR_2026,
) -> list[CalendarEvent]:
    """Return events between `start` and `end` (inclusive)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return [ev for ev in events if start <= ev.when_utc <= end]
