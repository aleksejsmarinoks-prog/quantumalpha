"""Aggregator tests — pure functions, fixtures-driven."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bot.daily_digest.aggregator import (
    aggregate_log_events,
    aggregate_equity_changes,
    aggregate_funding_rates,
    gather_calendar_today,
    gather_bot_health,
    gather_trade_trigger_status,
)


# ---------------------------------------------------------------------------
# aggregate_log_events
# ---------------------------------------------------------------------------

class TestAggregateLogEvents:

    def test_missing_log_file_returns_zeros(self, tmp_path):
        result = aggregate_log_events(tmp_path / "nonexistent.log")
        assert result["trades_opened"] == 0
        assert result["errors"] == 0
        assert result["total_lines_scanned"] == 0

    def test_counts_known_patterns(self, temp_log_file, utc_now):
        result = aggregate_log_events(temp_log_file, now=utc_now)
        assert result["trades_opened"] == 1
        assert result["trades_closed"] == 1
        assert result["errors"] == 1               # 1 within window
        assert result["warnings"] == 2             # DXY + telegram reconnect
        assert result["lv1_evals"] == 2
        assert result["funding_cycles"] == 2       # started + completed
        assert result["mean_rev_signals"] == 1
        assert result["dxy_missing"] == 1
        assert result["telegram_reconnects"] == 1

    def test_excludes_out_of_window_lines(self, temp_log_file, utc_now):
        result = aggregate_log_events(temp_log_file, window_hours=24, now=utc_now)
        # The 48h-old "ancient error" must NOT be counted
        assert result["errors"] == 1, f"got {result['errors']} errors, expected 1"

    def test_empty_log_file(self, tmp_path):
        log = tmp_path / "empty.log"
        log.write_text("", encoding="utf-8")
        result = aggregate_log_events(log)
        assert result["total_lines_scanned"] == 0
        assert all(v == 0 for k, v in result.items() if k != "total_lines_scanned")

    def test_lines_without_timestamp_still_counted(self, tmp_path, utc_now):
        log = tmp_path / "qa.log"
        log.write_text(
            "trade opened ETHUSDT\n"   # no timestamp — included by default
            "ERROR something broke\n",
            encoding="utf-8",
        )
        result = aggregate_log_events(log, now=utc_now)
        assert result["trades_opened"] == 1
        assert result["errors"] == 1

    def test_max_bytes_seek_with_large_file(self, tmp_path, utc_now):
        log = tmp_path / "huge.log"
        # Build a file > 1MB. First half is old garbage, second half has 1 valid event.
        old_garbage = "x" * 800_000 + "\n"
        recent_line = f"{utc_now - timedelta(hours=1):%Y-%m-%d %H:%M:%S} ERROR boom\n"
        log.write_text(old_garbage + recent_line, encoding="utf-8")
        result = aggregate_log_events(log, now=utc_now, max_bytes=500_000)
        # With seek, we should still pick up the recent ERROR
        assert result["errors"] >= 1


# ---------------------------------------------------------------------------
# aggregate_equity_changes
# ---------------------------------------------------------------------------

class TestAggregateEquityChanges:

    def test_missing_db_returns_nones(self, tmp_path):
        result = aggregate_equity_changes(tmp_path / "missing.db")
        assert result["start"] is None
        assert result["delta"] is None
        assert result["snapshot_count"] == 0

    def test_computes_start_end_delta(self, temp_equity_db, utc_now):
        result = aggregate_equity_changes(temp_equity_db, now=utc_now)
        assert result["start"] == 1000.00
        assert result["end"] == 1003.50
        assert result["delta"] == pytest.approx(3.50)
        assert result["delta_pct"] == pytest.approx(0.35, abs=0.01)
        assert result["snapshot_count"] == 5

    def test_computes_max_drawdown(self, temp_equity_db, utc_now):
        result = aggregate_equity_changes(temp_equity_db, now=utc_now)
        # Peak 1002.50, trough 998.00 → DD = (1002.50 - 998.00)/1002.50 ≈ 0.449%
        assert result["max_dd_pct"] == pytest.approx(0.449, abs=0.01)

    def test_empty_table(self, tmp_path):
        db = tmp_path / "equity.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE equity_snapshots (snapshot_utc TEXT, equity REAL)")
        conn.commit()
        conn.close()
        result = aggregate_equity_changes(db)
        assert result["snapshot_count"] == 0
        assert result["start"] is None

    def test_unknown_schema_returns_empty(self, tmp_path):
        db = tmp_path / "weird.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE random_table (foo INT)")
        conn.commit()
        conn.close()
        result = aggregate_equity_changes(db)
        assert result["start"] is None
        assert result["snapshot_count"] == 0


# ---------------------------------------------------------------------------
# aggregate_funding_rates
# ---------------------------------------------------------------------------

class TestAggregateFundingRates:

    def test_missing_db(self, tmp_path):
        assert aggregate_funding_rates(tmp_path / "missing.db") == {}

    def test_groups_by_symbol(self, temp_funding_db, utc_now):
        result = aggregate_funding_rates(temp_funding_db, now=utc_now)
        assert "ETHUSDT" in result
        assert "SOLUSDT" in result
        assert result["ETHUSDT"]["count"] == 3   # out-of-window excluded
        assert result["ETHUSDT"]["current"] == pytest.approx(0.00018)
        assert result["ETHUSDT"]["median_24h"] == pytest.approx(0.00015)

    def test_window_filter_excludes_old(self, temp_funding_db, utc_now):
        result = aggregate_funding_rates(temp_funding_db, window_hours=24, now=utc_now)
        # 48h-old ETHUSDT 0.99999 entry must NOT pollute median
        assert result["ETHUSDT"]["current"] != pytest.approx(0.99999)
        assert result["ETHUSDT"]["median_24h"] < 0.001

    def test_empty_table(self, tmp_path):
        db = tmp_path / "funding.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE funding_rate_history (fetched_utc TEXT, symbol TEXT, funding_rate REAL)")
        conn.commit()
        conn.close()
        assert aggregate_funding_rates(db) == {}


# ---------------------------------------------------------------------------
# gather_calendar_today
# ---------------------------------------------------------------------------

class TestGatherCalendarToday:

    def test_none_provider(self):
        assert gather_calendar_today(None) == []

    def test_filters_to_24h_window(self, calendar_provider_sample, utc_now):
        events = gather_calendar_today(calendar_provider_sample, now=utc_now)
        # 2 events should be in window (8h + 4h), 1 outside (26h)
        assert len(events) == 2
        names = {e["name"] for e in events}
        assert names == {"FOMC meeting", "ECB statement"}

    def test_sorts_by_time(self, calendar_provider_sample, utc_now):
        events = gather_calendar_today(calendar_provider_sample, now=utc_now)
        times = [e["time_utc"] for e in events]
        assert times == sorted(times)

    def test_provider_raises_returns_empty(self, utc_now):
        def bad_provider():
            raise RuntimeError("calendar service down")
        assert gather_calendar_today(bad_provider, now=utc_now) == []

    def test_handles_iso_string_timestamps(self, utc_now):
        def provider():
            return [{
                "time_utc": (utc_now + timedelta(hours=3)).isoformat(),
                "name": "CPI",
                "importance": "high",
            }]
        result = gather_calendar_today(provider, now=utc_now)
        assert len(result) == 1
        assert result[0]["name"] == "CPI"


# ---------------------------------------------------------------------------
# gather_bot_health
# ---------------------------------------------------------------------------

class TestGatherBotHealth:

    def test_none_provider_defaults(self):
        result = gather_bot_health(None)
        assert result["uptime_sec"] is None
        assert result["strategies"] == {}

    def test_passes_through_known_keys(self, state_provider_sample):
        result = gather_bot_health(state_provider_sample)
        assert result["uptime_sec"] == 86400 + 35 * 60
        assert result["memory_mb"] == pytest.approx(281.4)
        assert "funding_arb" in result["strategies"]
        assert result["strategies"]["liquidity_vortex"]["signal_count"] == 2880

    def test_provider_exception_returns_defaults(self):
        def bad_provider():
            raise ConnectionError("state service offline")
        result = gather_bot_health(bad_provider)
        assert result["uptime_sec"] is None


# ---------------------------------------------------------------------------
# gather_trade_trigger_status
# ---------------------------------------------------------------------------

class TestGatherTradeTriggerStatus:

    def test_systemctl_active(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="active\n", returncode=0)
            result = gather_trade_trigger_status()
            assert result["available"] is True
            assert result["active"] is True

    def test_systemctl_inactive(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="inactive\n", returncode=3)
            result = gather_trade_trigger_status()
            assert result["available"] is True
            assert result["active"] is False
            assert result["status"] == "inactive"

    def test_systemctl_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("systemctl missing")):
            result = gather_trade_trigger_status()
            assert result["available"] is False
            assert result["error"] is not None
