"""RSS source tests — mocks feedparser, no network."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from bot.trade_trigger.sources.rss_base import RSSWatcherBase, RSSWatcherConfig
from bot.trade_trigger.sources.government import (
    WhiteHouseWatcher, OFACWatcher, StateDeptWatcher, FedWatcher,
)
from bot.trade_trigger.models import Tier


# ---------------------------------------------------------------------------
# Mock feedparser response
# ---------------------------------------------------------------------------

def _mock_feed(entries):
    feed = MagicMock()
    feed.entries = entries
    feed.bozo = False
    return feed


def _entry(title, link="https://example.gov/x", summary="", guid=None, ts=None):
    """Make a feedparser-style entry dict."""
    e = {
        "title": title,
        "link": link,
        "summary": summary,
        "id": guid or link,
    }
    if ts:
        e["published_parsed"] = ts.timetuple()
    return e


# ---------------------------------------------------------------------------
# Concrete test watcher (underscore prefix to prevent pytest collection)
# ---------------------------------------------------------------------------

class _TestRSSWatcher(RSSWatcherBase):
    feed_url = "https://test.local/feed"
    source_domain = "test.local"
    source_tier = Tier.T1
    source_name = "test_rss"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRSSWatcherBase:

    async def test_poll_inserts_new_events(self, db, utc_now):
        watcher = _TestRSSWatcher(db)
        entries = [
            _entry("Trump signs executive order on AI", guid="wh-001",
                   summary="The President signed...", ts=utc_now),
            _entry("New sanctions on Iran shipping", guid="wh-002",
                   summary="OFAC announced...", ts=utc_now),
        ]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events = await watcher.poll_once()

        assert len(events) == 2
        assert all(e.source_domain == "test.local" for e in events)
        assert all(e.source_tier == Tier.T1 for e in events)
        # Verify in DB
        assert db.event_exists(events[0].raw_id)

    async def test_dedup_on_second_poll(self, db, utc_now):
        watcher = _TestRSSWatcher(db)
        entries = [
            _entry("Test announcement", guid="dup-001", ts=utc_now),
        ]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            first = await watcher.poll_once()
            second = await watcher.poll_once()

        assert len(first) == 1
        assert len(second) == 0  # dedup'd

    async def test_html_stripped_from_body(self, db):
        watcher = _TestRSSWatcher(db)
        entries = [
            _entry(
                "Press release",
                guid="html-1",
                summary="<p>The <strong>White House</strong> announced...</p>",
            ),
        ]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events = await watcher.poll_once()

        assert len(events) == 1
        assert "<p>" not in events[0].body
        assert "<strong>" not in events[0].body
        assert "White House announced" in events[0].body

    async def test_missing_title_skipped(self, db):
        watcher = _TestRSSWatcher(db)
        entries = [
            _entry("", guid="empty-1"),  # empty title
            _entry("Real news", guid="real-1"),
        ]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events = await watcher.poll_once()

        # Only one event made it (the one with title)
        assert len(events) == 1
        assert events[0].headline == "Real news"

    async def test_max_items_per_poll_respected(self, db):
        watcher = _TestRSSWatcher(db, config=RSSWatcherConfig(max_items_per_poll=3))
        entries = [_entry(f"News {i}", guid=f"n-{i}") for i in range(10)]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events = await watcher.poll_once()

        assert len(events) == 3

    async def test_fetch_error_does_not_crash(self, db):
        watcher = _TestRSSWatcher(db)
        with patch.object(watcher, "_fetch_feed", side_effect=ConnectionError("DNS fail")):
            events = await watcher.poll_once()

        assert events == []
        # Source health should reflect the failure
        rows = db.all_source_health()
        test_row = next((r for r in rows if r["source_name"] == "test_rss"), None)
        assert test_row is not None
        assert test_row["consecutive_fails"] >= 1
        assert "ConnectionError" in (test_row["last_error"] or "")

    async def test_long_headline_truncated(self, db):
        watcher = _TestRSSWatcher(db)
        long_title = "x" * 1000
        with patch.object(watcher, "_fetch_feed", return_value=[
            _entry(long_title, guid="long-1"),
        ]):
            events = await watcher.poll_once()
        assert len(events) == 1
        assert len(events[0].headline) <= 500

    async def test_dedup_id_deterministic(self, db):
        watcher = _TestRSSWatcher(db)
        entries = [_entry("Same news", link="https://test.local/a", guid="g-1")]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events1 = await watcher.poll_once()
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events2 = await watcher.poll_once()
        # Same GUID → same raw_id → second poll inserts nothing
        assert len(events1) == 1
        assert len(events2) == 0


# ---------------------------------------------------------------------------
# Government concrete watchers — just verify config
# ---------------------------------------------------------------------------

class TestGovernmentWatchers:

    def test_whitehouse_config(self, db):
        w = WhiteHouseWatcher(db)
        assert w.feed_url.startswith("https://")
        assert w.source_domain == "whitehouse.gov"
        assert w.source_tier == Tier.T1

    def test_ofac_config(self, db):
        w = OFACWatcher(db)
        assert "ofac" in w.feed_url.lower()
        assert w.source_domain == "ofac.treasury.gov"

    def test_state_dept_config(self, db):
        w = StateDeptWatcher(db)
        assert "state.gov" in w.feed_url.lower()
        assert w.source_domain == "state.gov"

    def test_fed_config(self, db):
        w = FedWatcher(db)
        assert "federalreserve.gov" in w.feed_url.lower()
        assert w.source_domain == "federalreserve.gov"

    def test_all_government_watchers_factory(self, db):
        from bot.trade_trigger.sources.government import all_government_watchers
        watchers = all_government_watchers(db)
        assert len(watchers) == 4
        names = {w.source_name for w in watchers}
        assert names == {"whitehouse", "ofac", "state_dept", "fed"}


# ---------------------------------------------------------------------------
# Integration: RSS event → classifier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRSSToClassifier:
    """Verify RSS events are correctly classified by heuristic rules."""

    async def test_whitehouse_iran_deal_event_classified(self, db, utc_now):
        from bot.trade_trigger.classifier import TradeTriggerClassifier

        watcher = WhiteHouseWatcher(db)
        entries = [
            _entry(
                "President announces positive Iran deal progress",
                guid="wh-iran-1",
                summary="Discussions with Iran are very positive, ongoing talks...",
                ts=utc_now,
            ),
        ]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events = await watcher.poll_once()
        assert len(events) == 1

        cls = TradeTriggerClassifier(enable_l2=False)
        result = cls.classify(events[0])
        # Classifier should map this to iran_us_deal_signal
        assert result.event_type == "iran_us_deal_signal"
        assert result.actionable is True

    async def test_fed_dovish_event_classified(self, db, utc_now):
        from bot.trade_trigger.classifier import TradeTriggerClassifier

        watcher = FedWatcher(db)
        entries = [
            _entry(
                "Powell signals dovish policy stance in Fed speech",
                guid="fed-1",
                summary="The Fed Chair indicated...",
                ts=utc_now,
            ),
        ]
        with patch.object(watcher, "_fetch_feed", return_value=entries):
            events = await watcher.poll_once()
        cls = TradeTriggerClassifier(enable_l2=False)
        result = cls.classify(events[0])
        assert result.event_type == "fed_dovish_signal"
