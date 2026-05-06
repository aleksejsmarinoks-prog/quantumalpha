"""Polymarket watcher tests — uses mocked HTTP client (no network)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

from bot.trade_trigger.sources.polymarket import (
    PolymarketWatcher, PolymarketClient, MarketSpec,
    WatcherConfig as PolymarketConfig, OddsShift,
)
from bot.trade_trigger.models import Tier


# ---------------------------------------------------------------------------
# Mocked Polymarket client — returns canned market dicts
# ---------------------------------------------------------------------------

class MockPolymarketClient:
    """Stub client. Tests configure responses via set_market()."""

    def __init__(self):
        self._markets: dict = {}

    def set_market(self, slug: str, market_dict: dict) -> None:
        self._markets[slug] = market_dict

    async def fetch_market_by_slug(self, slug: str) -> Optional[dict]:
        return self._markets.get(slug)

    @staticmethod
    def extract_outcome_price(market: dict, outcome_name: str) -> Optional[float]:
        # Reuse real implementation
        return PolymarketClient.extract_outcome_price(market, outcome_name)

    async def close(self):
        pass


def _make_market(yes_price: float, no_price: Optional[float] = None) -> dict:
    """Helper: create a market dict in Gamma's parallel-arrays shape."""
    if no_price is None:
        no_price = round(1.0 - yes_price, 4)
    return {
        "outcomes": ["Yes", "No"],
        "outcomePrices": [str(yes_price), str(no_price)],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    return MockPolymarketClient()


@pytest.fixture
def hormuz_watchlist():
    """Single market spec for testing — Hormuz closure, 'up' direction = escalation."""
    return [
        MarketSpec(
            slug="will-iran-close-the-strait-of-hormuz-in-may-2026",
            outcome="Yes",
            event_type="hormuz_escalation",
            direction="up",
            shift_threshold=0.05,
            label="Iran-Hormuz-closure",
        ),
    ]


@pytest.fixture
def watcher(db, mock_client, hormuz_watchlist):
    return PolymarketWatcher(
        db=db,
        client=mock_client,
        watchlist=hormuz_watchlist,
        config=PolymarketConfig(
            poll_interval_seconds=0,  # not used in unit tests
            shift_window_minutes=15,
        ),
    )


# ---------------------------------------------------------------------------
# Tests — extraction
# ---------------------------------------------------------------------------

class TestPriceExtraction:

    def test_parallel_arrays_shape(self):
        market = _make_market(yes_price=0.18)
        price = PolymarketClient.extract_outcome_price(market, "Yes")
        assert price == pytest.approx(0.18)

    def test_tokens_shape(self):
        market = {
            "tokens": [
                {"outcome": "Yes", "price": "0.42"},
                {"outcome": "No", "price": "0.58"},
            ],
        }
        price = PolymarketClient.extract_outcome_price(market, "Yes")
        assert price == pytest.approx(0.42)

    def test_case_insensitive_outcome_match(self):
        market = _make_market(yes_price=0.30)
        assert PolymarketClient.extract_outcome_price(market, "YES") == pytest.approx(0.30)
        assert PolymarketClient.extract_outcome_price(market, "yes") == pytest.approx(0.30)

    def test_unknown_outcome_returns_none(self):
        market = _make_market(yes_price=0.30)
        assert PolymarketClient.extract_outcome_price(market, "Maybe") is None

    def test_malformed_market_returns_none(self):
        assert PolymarketClient.extract_outcome_price({}, "Yes") is None
        assert PolymarketClient.extract_outcome_price(
            {"outcomes": ["Yes"], "outcomePrices": []}, "Yes",
        ) is None


# ---------------------------------------------------------------------------
# Tests — single poll cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPollCycle:

    async def test_first_poll_no_shift_no_event(self, watcher, mock_client, utc_now):
        """First observation has nothing to compare to — no event."""
        slug = watcher.watchlist[0].slug
        mock_client.set_market(slug, _make_market(yes_price=0.18))

        events = await watcher.poll_once(now=utc_now)
        assert events == []

        # But observation IS persisted
        latest = watcher.db.latest_polymarket_odds(slug, "Yes")
        assert latest is not None
        assert latest[0] == pytest.approx(0.18)

    async def test_small_shift_no_event(self, watcher, mock_client, utc_now):
        """Shift below threshold (5pp) → no event."""
        slug = watcher.watchlist[0].slug

        # T0: 18%
        mock_client.set_market(slug, _make_market(yes_price=0.18))
        await watcher.poll_once(now=utc_now)

        # T+5min: 21% (only +3pp, below 5pp threshold)
        mock_client.set_market(slug, _make_market(yes_price=0.21))
        events = await watcher.poll_once(now=utc_now + timedelta(minutes=5))
        assert events == []

    async def test_significant_up_shift_emits_event(
        self, watcher, mock_client, utc_now,
    ):
        """Shift +5pp+ in 'up' direction → emits NewsEvent for hormuz_escalation."""
        slug = watcher.watchlist[0].slug

        # T0: baseline 18%
        mock_client.set_market(slug, _make_market(yes_price=0.18))
        await watcher.poll_once(now=utc_now)

        # T+8min: spike to 35% — that's +17pp
        mock_client.set_market(slug, _make_market(yes_price=0.35))
        events = await watcher.poll_once(now=utc_now + timedelta(minutes=8))

        assert len(events) == 1
        ev = events[0]
        assert "Polymarket" in ev.headline
        assert "Iran-Hormuz-closure" in ev.headline
        assert ev.source_domain == "polymarket.com"
        assert ev.source_tier == Tier.T1
        assert "18%" in ev.headline and "35%" in ev.headline

    async def test_down_shift_filtered_when_direction_is_up(
        self, watcher, mock_client, utc_now,
    ):
        """If watchlist spec.direction='up', a downward shift does NOT emit."""
        slug = watcher.watchlist[0].slug

        # T0: high 35%
        mock_client.set_market(slug, _make_market(yes_price=0.35))
        await watcher.poll_once(now=utc_now)

        # T+5min: drops to 18% (-17pp DOWN)
        mock_client.set_market(slug, _make_market(yes_price=0.18))
        events = await watcher.poll_once(now=utc_now + timedelta(minutes=5))

        # Direction is 'up' only — no emit
        assert events == []

    async def test_either_direction_catches_both_ways(
        self, db, mock_client, utc_now,
    ):
        spec = MarketSpec(
            slug="bidirectional-test",
            outcome="Yes",
            event_type="vix_spike_extreme",
            direction="either",
            shift_threshold=0.05,
            label="bidi-test",
        )
        w = PolymarketWatcher(
            db=db, client=mock_client, watchlist=[spec],
            config=PolymarketConfig(shift_window_minutes=15),
        )

        mock_client.set_market(spec.slug, _make_market(yes_price=0.20))
        await w.poll_once(now=utc_now)

        # Big down move
        mock_client.set_market(spec.slug, _make_market(yes_price=0.05))
        events = await w.poll_once(now=utc_now + timedelta(minutes=5))
        assert len(events) == 1

    async def test_market_not_available(self, watcher, mock_client, utc_now):
        """If client returns None — silent skip, no error."""
        events = await watcher.poll_once(now=utc_now)
        assert events == []


# ---------------------------------------------------------------------------
# Tests — synthetic event format
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyntheticEvent:

    async def test_raw_id_deterministic(
        self, watcher, mock_client, utc_now,
    ):
        """Same shift at same minute produces same raw_id (dedup safety)."""
        slug = watcher.watchlist[0].slug

        # Build T0
        mock_client.set_market(slug, _make_market(yes_price=0.18))
        await watcher.poll_once(now=utc_now)

        # T+8min spike, twice with same params
        mock_client.set_market(slug, _make_market(yes_price=0.35))
        events1 = await watcher.poll_once(now=utc_now + timedelta(minutes=8))
        events2 = await watcher.poll_once(now=utc_now + timedelta(minutes=8))

        # raw_id is identical when shift, price, and minute match
        assert len(events1) == 1
        assert len(events2) == 1
        assert events1[0].raw_id == events2[0].raw_id

    async def test_event_classifies_to_hormuz_escalation(
        self, watcher, mock_client, utc_now,
    ):
        """Synthetic event must pass through real classifier as expected event_type."""
        from bot.trade_trigger.classifier import TradeTriggerClassifier

        slug = watcher.watchlist[0].slug

        mock_client.set_market(slug, _make_market(yes_price=0.18))
        await watcher.poll_once(now=utc_now)

        mock_client.set_market(slug, _make_market(yes_price=0.40))
        events = await watcher.poll_once(now=utc_now + timedelta(minutes=8))
        assert len(events) == 1

        # Run synthetic event through real heuristic classifier
        cls = TradeTriggerClassifier(enable_l2=False)
        result = cls.classify(events[0])
        assert result.event_type == "hormuz_escalation"
        assert result.actionable is True


# ---------------------------------------------------------------------------
# Tests — source health bookkeeping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSourceHealthBookkeeping:

    async def test_health_updated_on_success(self, watcher, mock_client, utc_now):
        slug = watcher.watchlist[0].slug
        mock_client.set_market(slug, _make_market(yes_price=0.18))
        await watcher.poll_once(now=utc_now)

        stats = watcher.db.stats()
        assert stats["sources_count"] >= 1
