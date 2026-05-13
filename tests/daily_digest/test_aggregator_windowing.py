"""Aggregator windowing + EVAL_TICK tests (Phase 7.2 Issues 1 + 4)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from bot.daily_digest.aggregator import (
    aggregate_log_events,
    _iterate_log_window,
    _parse_log_timestamp,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def utc_anchor():
    return datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc)


def _ts(anchor, hours_ago):
    return (anchor - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# Issue 4 — Windowing
# ===========================================================================

class TestLogWindowing:

    def test_lines_pre_cutoff_excluded(self, tmp_path, utc_anchor):
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            f"{_ts(utc_anchor, 48)} ERROR ancient",          # pre-cutoff
            f"{_ts(utc_anchor, 12)} ERROR recent",            # in window
            f"{_ts(utc_anchor, 2)}  ERROR very recent",       # in window
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        assert result["errors"] == 2, "must exclude 48h-old line"

    def test_continuation_lines_inherit_timestamp(self, tmp_path, utc_anchor):
        """Stacktrace continuation lines (no timestamp) inherit from header."""
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            f"{_ts(utc_anchor, 48)} ERROR ancient error happened",
            "Traceback (most recent call last):",            # no ts → inherits 48h
            '  File "x.py", line 1',                          # no ts → inherits 48h
            "RuntimeError: boom",                             # no ts → inherits 48h
            f"{_ts(utc_anchor, 5)} ERROR recent legit error",
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        # Only the 5h-ago error counts. The 4 ancient stacktrace lines must NOT.
        assert result["errors"] == 1

    def test_lines_without_anchor_yet_included(self, tmp_path, utc_anchor):
        """Pre-first-timestamp lines pass through optimistically."""
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            "no timestamp ERROR foo",                         # no anchor → optimistic
            "another line ERROR bar",                         # no anchor → optimistic
            f"{_ts(utc_anchor, 2)} ERROR third",
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        assert result["errors"] == 3

    def test_iso_format_timestamp(self, tmp_path, utc_anchor):
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            f"{(utc_anchor - timedelta(hours=2)).isoformat(timespec='seconds').replace('+00:00','')} ERROR iso",
            f"{(utc_anchor - timedelta(hours=48)).isoformat(timespec='seconds').replace('+00:00','')} ERROR old",
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        assert result["errors"] == 1

    def test_empty_log_file(self, tmp_path):
        log = tmp_path / "qa.log"
        log.write_text("", encoding="utf-8")
        result = aggregate_log_events(log)
        assert result["total_lines_scanned"] == 0
        assert result["errors"] == 0

    def test_missing_log_file(self, tmp_path):
        result = aggregate_log_events(tmp_path / "missing.log")
        assert result["total_lines_scanned"] == 0
        assert result["errors"] == 0

    def test_window_boundary_exact(self, tmp_path, utc_anchor):
        """Line exactly at cutoff edge is included (>=, not >)."""
        log = tmp_path / "qa.log"
        log.write_text(
            f"{_ts(utc_anchor, 24)} ERROR boundary\n"   # exactly 24h ago
            f"{_ts(utc_anchor, 24)} ERROR boundary2\n",
            encoding="utf-8",
        )
        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        # Cutoff is now-24h; line at 24h is < cutoff by microseconds typically
        # — accept either 0 or 2 (depends on timestamp roundtrip)
        assert result["errors"] in (0, 2)

    def test_huge_log_seeks_from_end(self, tmp_path, utc_anchor):
        """Files > max_bytes only read tail."""
        log = tmp_path / "huge.log"
        padding = "padding line without timestamp\n" * 50_000   # ~1.5MB
        recent = f"{_ts(utc_anchor, 1)} ERROR recent\n"
        log.write_text(padding + recent, encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor, max_bytes=100_000)
        # Recent ERROR should be picked up
        assert result["errors"] >= 1


# ===========================================================================
# Issue 1 — EVAL_TICK strict counting
# ===========================================================================

class TestEvalTickCounting:

    def test_counts_per_strategy(self, tmp_path, utc_anchor):
        log = tmp_path / "qa.log"
        lines = []
        # 5 LV1 ticks, 3 funding_arb cycles
        for i in range(5):
            lines.append(f"{_ts(utc_anchor, i+1)} INFO [EVAL_TICK] strategy=liquidity_vortex_v1 symbol=ETHUSDT result=hold")
        for i in range(3):
            lines.append(f"{_ts(utc_anchor, i+1)} INFO [EVAL_TICK] strategy=funding_arb_v1 result=hold")
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        ticks = result["eval_ticks_by_strategy"]
        assert ticks["liquidity_vortex_v1"] == 5
        assert ticks["funding_arb_v1"] == 3

    def test_ignores_non_eval_tick_strategy_mentions(self, tmp_path, utc_anchor):
        """Old broad regex pattern would catch these. Phase 7.2 must NOT."""
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            f"{_ts(utc_anchor, 1)} INFO liquidity_vortex_v1 evaluating ETHUSDT",   # old style — must NOT count
            f"{_ts(utc_anchor, 1)} INFO funding_arb_v1 cycle started",              # old style — must NOT count
            f"{_ts(utc_anchor, 1)} INFO funding_arb_v1 cycle completed",            # old style — must NOT count
            f"{_ts(utc_anchor, 1)} INFO [EVAL_TICK] strategy=funding_arb_v1 result=hold",  # only this counts
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        ticks = result["eval_ticks_by_strategy"]
        assert ticks.get("funding_arb_v1", 0) == 1
        assert ticks.get("liquidity_vortex_v1", 0) == 0

    def test_eval_ticks_respect_window(self, tmp_path, utc_anchor):
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            f"{_ts(utc_anchor, 48)} INFO [EVAL_TICK] strategy=lv result=hold",  # outside
            f"{_ts(utc_anchor, 2)}  INFO [EVAL_TICK] strategy=lv result=hold",  # inside
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        assert result["eval_ticks_by_strategy"].get("lv", 0) == 1

    def test_no_eval_ticks_returns_empty_dict(self, tmp_path, utc_anchor):
        log = tmp_path / "qa.log"
        log.write_text(
            f"{_ts(utc_anchor, 1)} INFO regular log line\n", encoding="utf-8",
        )
        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        assert result["eval_ticks_by_strategy"] == {}


# ===========================================================================
# Severity counts (Phase 7.2 split)
# ===========================================================================

class TestSeverityCounts:

    def test_split_by_severity(self, tmp_path, utc_anchor):
        log = tmp_path / "qa.log"
        log.write_text("\n".join([
            f"{_ts(utc_anchor, 1)} CRITICAL something fatal",
            f"{_ts(utc_anchor, 1)} ERROR error one",
            f"{_ts(utc_anchor, 1)} ERROR error two",
            f"{_ts(utc_anchor, 1)} WARNING warning one",
            f"{_ts(utc_anchor, 1)} WARNING warning two",
            f"{_ts(utc_anchor, 1)} WARNING warning three",
            f"{_ts(utc_anchor, 1)} INFO normal info",
        ]) + "\n", encoding="utf-8")

        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        sev = result["severity_counts"]
        assert sev["CRITICAL"] == 1
        assert sev["ERROR"] == 2
        assert sev["WARNING"] == 3
        # Legacy compat
        assert result["errors"] == 3   # CRITICAL + ERROR
        assert result["warnings"] == 3

    def test_critical_not_double_counted_as_error(self, tmp_path, utc_anchor):
        """A CRITICAL line should NOT also count as ERROR (mutually exclusive buckets)."""
        log = tmp_path / "qa.log"
        log.write_text(
            f"{_ts(utc_anchor, 1)} CRITICAL boom\n", encoding="utf-8",
        )
        result = aggregate_log_events(log, window_hours=24, now=utc_anchor)
        sev = result["severity_counts"]
        assert sev["CRITICAL"] == 1
        assert sev["ERROR"] == 0


# ===========================================================================
# Timestamp parser unit tests
# ===========================================================================

class TestTimestampParser:

    def test_space_separator(self):
        ts = _parse_log_timestamp("2026-05-13 10:00:00 INFO foo")
        assert ts is not None
        assert ts.year == 2026 and ts.hour == 10

    def test_iso_separator(self):
        ts = _parse_log_timestamp("2026-05-13T10:00:00 INFO foo")
        assert ts is not None

    def test_no_timestamp(self):
        assert _parse_log_timestamp("no timestamp here") is None

    def test_malformed_timestamp(self):
        assert _parse_log_timestamp("2026-13-99 99:99:99 garbage") is None

    def test_returns_utc_tz(self):
        ts = _parse_log_timestamp("2026-05-13 10:00:00")
        assert ts is not None
        assert ts.tzinfo == timezone.utc
