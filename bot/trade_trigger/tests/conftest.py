"""Pytest fixtures shared across qa_trade_trigger tests."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pytest

# Ensure parent path on sys.path so package imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def tmp_db_path(tmp_path):
    """Temp SQLite path that auto-cleans after test."""
    return str(tmp_path / "trade_trigger_test.db")


@pytest.fixture
def db(tmp_db_path):
    """Fresh TradeTriggerDB instance for each test."""
    from bot.trade_trigger.db import TradeTriggerDB
    return TradeTriggerDB(tmp_db_path)


@pytest.fixture
def utc_now():
    """Frozen 'now' for deterministic time-based tests."""
    return datetime(2026, 5, 6, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def event_factory(utc_now):
    """Factory producing NewsEvent with sensible defaults."""
    from bot.trade_trigger.models import NewsEvent, Tier

    def _factory(
        headline: str = "Test event",
        body: str = "Test body content",
        source_domain: str = "reuters.com",
        source_tier: Tier = Tier.T1,
        published_at=None,
        raw_id: Optional[str] = None,
    ) -> NewsEvent:
        published_at = published_at or utc_now
        return NewsEvent(
            headline=headline,
            body=body,
            source_url=f"https://{source_domain}/test",
            source_domain=source_domain,
            source_tier=source_tier,
            published_at=published_at,
            fetched_at=published_at,
            raw_id=raw_id or f"test_{hash((headline, source_domain)) & 0xffffff:x}",
        )

    return _factory


class MockPriceProvider:
    """Test double for LivePriceProvider."""

    def __init__(self, klines_by_symbol: Optional[dict] = None):
        self.klines = klines_by_symbol or {}

    async def get_klines_1h(
        self, symbol: str, count: int = 24,
    ) -> Optional[List[float]]:
        normalized = symbol.replace("/", "").upper()
        return self.klines.get(normalized)


@pytest.fixture
def mock_price_provider():
    return MockPriceProvider
