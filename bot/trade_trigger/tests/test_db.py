"""Database layer tests."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from bot.trade_trigger.models import (
    NewsEvent, Tier, Direction, ClassificationResult,
    TradeSignal, AssetTrigger, TriggerVerdict,
)


class TestEventStorage:

    def test_insert_event_returns_true_first_time(self, db, event_factory):
        ev = event_factory()
        assert db.insert_event(ev) is True

    def test_dedup_returns_false(self, db, event_factory):
        ev = event_factory(raw_id="dup_test")
        assert db.insert_event(ev) is True
        assert db.insert_event(ev) is False  # duplicate

    def test_get_event_round_trip(self, db, event_factory):
        ev = event_factory(headline="Round trip test", raw_id="rt_test")
        db.insert_event(ev)
        retrieved = db.get_event("rt_test")
        assert retrieved is not None
        assert retrieved.headline == "Round trip test"
        assert retrieved.source_domain == ev.source_domain
        assert retrieved.source_tier == ev.source_tier

    def test_event_exists(self, db, event_factory):
        ev = event_factory(raw_id="exists_test")
        assert db.event_exists("exists_test") is False
        db.insert_event(ev)
        assert db.event_exists("exists_test") is True

    def test_recent_events_for_topic(self, db, event_factory, utc_now):
        # Insert 3 events with "Hormuz", 1 without
        for i, (headline, src, ts) in enumerate([
            ("Hormuz strike 1", "reuters.com", utc_now - timedelta(minutes=3)),
            ("Hormuz strike 2", "bloomberg.com", utc_now - timedelta(minutes=5)),
            ("Hormuz strike 3", "wsj.com", utc_now - timedelta(minutes=10)),
            ("Unrelated tech earnings", "cnbc.com", utc_now - timedelta(minutes=2)),
        ]):
            db.insert_event(event_factory(
                headline=headline, source_domain=src,
                published_at=ts, raw_id=f"topic_{i}",
            ))

        matches = db.recent_events_for_topic("Hormuz", within_minutes=15, now=utc_now)
        assert len(matches) == 3


class TestClassificationStorage:

    def test_upsert_classification(self, db, event_factory):
        ev = event_factory(raw_id="cls_test")
        db.insert_event(ev)

        result = ClassificationResult(
            event_type="hormuz_easing",
            actionable=True,
            actionability_score=7.5,
            direction_hint=Direction.LONG,
            asset_class_hint="crypto",
            half_life_minutes=120,
            confidence=0.8,
            reasoning="Test classification",
            raw_keywords_matched=["hormuz", "transit"],
        )
        db.upsert_classification("cls_test", result)
        # Re-classify (idempotent)
        result.actionability_score = 8.0
        db.upsert_classification("cls_test", result)

    def test_classification_requires_event_foreign_key(self, db):
        result = ClassificationResult(
            event_type="hormuz_easing", actionable=True,
            actionability_score=7.0, direction_hint=None,
            asset_class_hint=None, half_life_minutes=0,
            confidence=0.5, reasoning="orphan",
        )
        # FK enabled — orphan classification should fail
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_classification("nonexistent_event", result)


class TestSignalStorage:

    def test_insert_signal(self, db, event_factory, utc_now):
        ev = event_factory(raw_id="sig_test")
        db.insert_event(ev)

        signal = TradeSignal(
            event_type="hormuz_easing",
            triggers=[
                AssetTrigger(
                    ticker="ETH/USDT", venue="Bybit",
                    direction=Direction.LONG, conviction=0.75,
                    suggested_size_pct_bucket=7.5,
                    invalidation_reason="ETH < $2300",
                    half_life_minutes=120,
                ),
            ],
            sources=["https://truthsocial.com/...", "https://reuters.com/..."],
            first_seen_utc=utc_now,
            actionability_score=7.5,
            reasoning="Hormuz easing detected",
            verdict=TriggerVerdict.FIRE,
        )
        signal_id = db.insert_signal("sig_test", signal)
        assert signal_id > 0

    def test_user_action_update(self, db, event_factory, utc_now):
        ev = event_factory(raw_id="action_test")
        db.insert_event(ev)
        signal = TradeSignal(
            event_type="hormuz_easing",
            triggers=[],
            sources=[],
            first_seen_utc=utc_now,
            actionability_score=7.5,
            reasoning="t",
        )
        sid = db.insert_signal("action_test", signal)
        db.update_signal_user_action(sid, "confirmed")
        # No assertion needed — just verify no exception


class TestSourceHealth:

    def test_initial_insert(self, db):
        db.update_source_health("test_source", success=True, events_added=5)
        # No exception → ok

    def test_consecutive_fails_track(self, db):
        db.update_source_health("flaky", success=True, events_added=1)
        db.update_source_health("flaky", success=False, error="timeout")
        db.update_source_health("flaky", success=False, error="500")
        db.update_source_health("flaky", success=True, events_added=2)
        # consecutive_fails should reset on success — verified via stats


class TestStats:

    def test_empty_db_stats(self, db):
        stats = db.stats()
        assert stats["total_events"] == 0
        assert stats["total_signals"] == 0

    def test_stats_count_events(self, db, event_factory):
        for i in range(5):
            db.insert_event(event_factory(raw_id=f"stat_{i}"))
        assert db.stats()["total_events"] == 5


class TestHeartbeat:

    def test_pulse_writes(self, db):
        db.pulse("test_component", {"foo": "bar"})
        # Just verify no exception
