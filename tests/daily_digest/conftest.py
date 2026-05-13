"""Pytest fixtures for daily_digest tests. No network, no real Anthropic SDK."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure project root is on sys.path so `bot.daily_digest` imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def utc_now():
    """Stable UTC anchor for time-dependent tests."""
    return datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def temp_log_file(tmp_path, utc_now):
    """Creates a sample log file with realistic content."""
    log_path = tmp_path / "qa.log"
    lines = [
        f"{utc_now - timedelta(hours=23):%Y-%m-%d %H:%M:%S},123 INFO trade opened ETHUSDT long",
        f"{utc_now - timedelta(hours=22):%Y-%m-%d %H:%M:%S},123 INFO funding_arb cycle started",
        f"{utc_now - timedelta(hours=20):%Y-%m-%d %H:%M:%S},123 INFO funding_arb cycle completed",
        f"{utc_now - timedelta(hours=18):%Y-%m-%d %H:%M:%S},456 WARNING DX-Y.NYB missing",
        f"{utc_now - timedelta(hours=15):%Y-%m-%d %H:%M:%S},123 INFO LV1 eval tick",
        f"{utc_now - timedelta(hours=15):%Y-%m-%d %H:%M:%S},124 INFO LV1 eval tick",
        f"{utc_now - timedelta(hours=12):%Y-%m-%d %H:%M:%S},789 ERROR Bybit connection refused",
        f"{utc_now - timedelta(hours=10):%Y-%m-%d %H:%M:%S},234 INFO position closed ETHUSDT",
        f"{utc_now - timedelta(hours=8):%Y-%m-%d %H:%M:%S},111 INFO mean_reversion signal generated",
        f"{utc_now - timedelta(hours=5):%Y-%m-%d %H:%M:%S},222 WARNING Telegram ServerDisconnectedError",
        f"{utc_now - timedelta(hours=2):%Y-%m-%d %H:%M:%S},333 INFO scheduler cycle completed",
        # An out-of-window line (older than 24h) — should be excluded
        f"{utc_now - timedelta(hours=48):%Y-%m-%d %H:%M:%S},000 ERROR ancient error",
    ]
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


@pytest.fixture
def temp_equity_db(tmp_path, utc_now):
    """Creates a sample equity SQLite DB."""
    db_path = tmp_path / "equity.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE equity_snapshots (
            id INTEGER PRIMARY KEY,
            snapshot_utc TEXT NOT NULL,
            equity REAL NOT NULL
        )
    """)
    snapshots = [
        (utc_now - timedelta(hours=24), 1000.00),
        (utc_now - timedelta(hours=18), 1002.50),
        (utc_now - timedelta(hours=12), 998.00),    # mid-window dip → drawdown
        (utc_now - timedelta(hours=6),  1005.00),
        (utc_now - timedelta(minutes=10), 1003.50),
    ]
    for ts, eq in snapshots:
        conn.execute(
            "INSERT INTO equity_snapshots (snapshot_utc, equity) VALUES (?, ?)",
            (ts.isoformat(), eq),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def temp_funding_db(tmp_path, utc_now):
    """Creates a sample funding rates SQLite DB matching production schema."""
    db_path = tmp_path / "funding.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE funding_rate_history (
            id INTEGER PRIMARY KEY,
            fetched_utc TEXT NOT NULL,
            symbol TEXT NOT NULL,
            funding_rate REAL NOT NULL,
            annualized_pct REAL,
            last_price REAL
        )
    """)
    samples = [
        ("ETHUSDT",  utc_now - timedelta(hours=20), 0.00012),
        ("ETHUSDT",  utc_now - timedelta(hours=12), 0.00015),
        ("ETHUSDT",  utc_now - timedelta(hours=4),  0.00018),
        ("SOLUSDT",  utc_now - timedelta(hours=20), 0.00021),
        ("SOLUSDT",  utc_now - timedelta(hours=12), 0.00025),
        ("SOLUSDT",  utc_now - timedelta(hours=4),  0.00030),
        # Out of window
        ("ETHUSDT",  utc_now - timedelta(hours=48), 0.99999),
    ]
    for sym, ts, rate in samples:
        conn.execute(
            "INSERT INTO funding_rate_history (fetched_utc, symbol, funding_rate) "
            "VALUES (?, ?, ?)",
            (ts.isoformat(), sym, rate),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def calendar_provider_sample(utc_now):
    """Calendar provider with 2 events in next 24h + 1 outside."""
    def _provider():
        return [
            {
                "time_utc": utc_now + timedelta(hours=8),
                "name": "FOMC meeting",
                "importance": "high",
            },
            {
                "time_utc": utc_now + timedelta(hours=26),  # outside 24h window
                "name": "CPI release",
                "importance": "high",
            },
            {
                "time_utc": utc_now + timedelta(hours=4),
                "name": "ECB statement",
                "importance": "medium",
            },
        ]
    return _provider


@pytest.fixture
def state_provider_sample():
    """Bot state provider with sample dict."""
    def _provider():
        return {
            "uptime_sec": 86400 + 35 * 60,  # 1d 35m
            "memory_mb": 281.4,
            "restart_count": 0,
            "provider_healthy": True,
            "open_positions": 0,
            "strategies": {
                "funding_arb": {"pnl_24h": 0.0, "signal_count": 96, "status": "active"},
                "mean_reversion": {"pnl_24h": 0.0, "signal_count": 288, "status": "active"},
                "liquidity_vortex": {"pnl_24h": 0.0, "signal_count": 2880, "status": "active"},
                "dca_dips": {"pnl_24h": 0.0, "signal_count": 24, "status": "active"},
                "cvd_divergence": {"pnl_24h": 0.0, "signal_count": 0, "status": "disabled"},
            },
        }
    return _provider


@pytest.fixture
def mock_anthropic_client():
    """Async-mocked Anthropic client. Returns canned digest response."""
    mock_client = MagicMock()

    # Default success response
    response = MagicMock()
    response.content = [MagicMock(text=(
        "🌅 *QuantumAlpha Daily Digest* — 11 May 2026\n\n"
        "📊 *Last 24h:*\n"
        "  • Trades opened: 1 (closed: 1)\n"
        "  • Equity: $1,000.00 → $1,003.50 (Δ +$3.50, +0.35%)\n"
        "  • Max DD: 0.45%\n"
        "  • Strategy activity: lv1 2 evals, mean_rev 1 signals, funding_arb 1 cycles\n\n"
        "📈 *Current state:*\n"
        "  • Open positions: 0\n"
        "  • Bot uptime: 1d 0h\n"
        "  • Memory: 281 MB\n\n"
        "🔮 *Today's catalysts:*\n"
        "  • 14:00 UTC ECB statement (medium)\n"
        "  • 18:00 UTC FOMC meeting — vol expansion likely\n\n"
        "⚠️ *Items needing attention:*\n"
        "  • DXY (DX-Y.NYB) missing — fallback active\n\n"
        "🎯 *Recommended action:*\n"
        "Watch /strategies around 18:30 UTC after FOMC."
    ))]
    response.usage = MagicMock(input_tokens=850, output_tokens=320)

    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=response)
    return mock_client
