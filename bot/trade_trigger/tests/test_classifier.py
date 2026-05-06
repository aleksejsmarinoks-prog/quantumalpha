"""
Classifier tests — pytest version.
Includes the real Hormuz event from May 3, 2026 as critical regression test.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.trade_trigger.models import NewsEvent, Tier, Direction
from bot.trade_trigger.classifier import (
    HeuristicScorer, TradeTriggerClassifier, L1_THRESHOLD,
)
from bot.trade_trigger.trade_trigger_mapping import (
    get_triggers_for_event, EXCLUDED_TICKERS, list_supported_events,
    is_event_supported,
)


# ---------------------------------------------------------------------------
# Event fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hormuz_easing_event():
    """REAL EVENT — May 3, 2026, ~22:00 UTC. Critical regression test."""
    return NewsEvent(
        headline="Trump: U.S. will guide stranded ships through Strait of Hormuz",
        body=(
            "President Trump said Sunday that the United States will guide "
            "stranded ships through the Strait of Hormuz, signaling a major "
            "shift in U.S.-Iran diplomatic posture. Trump described ongoing "
            "discussions with Iran as 'very positive' and indicated they could "
            "lead to favorable outcomes."
        ),
        source_url="https://truthsocial.com/@realDonaldTrump/posts/...",
        source_domain="truthsocial.com",
        source_tier=Tier.T1,
        published_at=datetime(2026, 5, 3, 22, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 3, 22, 5, tzinfo=timezone.utc),
        raw_id="hormuz_easing_20260503",
    )


@pytest.fixture
def hormuz_escalation_event():
    return NewsEvent(
        headline="Iran closes Strait of Hormuz to all shipping after U.S. strikes",
        body=(
            "Iran announced it will close the Strait of Hormuz to all "
            "international shipping following overnight U.S. airstrikes."
        ),
        source_url="https://www.reuters.com/world/middle-east/...",
        source_domain="reuters.com",
        source_tier=Tier.T1,
        published_at=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 6, 8, 3, tzinfo=timezone.utc),
        raw_id="hormuz_escalation_test",
    )


@pytest.fixture
def fed_dovish_event():
    return NewsEvent(
        headline="Warsh signals aggressive rate cuts in first speech as nominee",
        body="Kevin Warsh said the Fed should cut rates faster.",
        source_url="https://www.federalreserve.gov/...",
        source_domain="federalreserve.gov",
        source_tier=Tier.T1,
        published_at=datetime(2026, 5, 6, 14, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 6, 14, 2, tzinfo=timezone.utc),
        raw_id="warsh_dovish_test",
    )


@pytest.fixture
def banned_source_event():
    return NewsEvent(
        headline="Hormuz strike imminent, sources say",
        body="Anonymous sources reported imminent Hormuz event.",
        source_url="https://news24.com/...",
        source_domain="news24.com",
        source_tier=Tier.BANNED,
        published_at=datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 6, 10, 1, tzinfo=timezone.utc),
        raw_id="banned_test",
    )


@pytest.fixture
def stable_depeg_event():
    return NewsEvent(
        headline="USDC loses peg, trades at $0.92 amid Circle reserve concerns",
        body="USDC broke its dollar peg, falling to $0.92 amid concerns.",
        source_url="https://www.bloomberg.com/...",
        source_domain="bloomberg.com",
        source_tier=Tier.T1,
        published_at=datetime(2026, 5, 6, 9, 30, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 6, 9, 32, tzinfo=timezone.utc),
        raw_id="usdc_depeg_test",
    )


@pytest.fixture
def noise_event():
    return NewsEvent(
        headline="Tech earnings season kicks off Tuesday",
        body="Several large-cap tech companies will report earnings.",
        source_url="https://www.cnbc.com/...",
        source_domain="cnbc.com",
        source_tier=Tier.T1,
        published_at=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 5, 6, 12, 1, tzinfo=timezone.utc),
        raw_id="noise_test",
    )


@pytest.fixture
def heuristic_classifier():
    return TradeTriggerClassifier(enable_l2=False)


# ---------------------------------------------------------------------------
# Tests — heuristic L1 only (no API calls)
# ---------------------------------------------------------------------------

class TestHeuristicClassifier:

    def test_hormuz_easing_real_event(self, heuristic_classifier, hormuz_easing_event):
        """REGRESSION — the May 3 event we missed must be caught."""
        result = heuristic_classifier.classify(hormuz_easing_event)
        assert result.event_type == "hormuz_easing"
        assert result.actionable is True
        assert result.actionability_score >= L1_THRESHOLD

    def test_hormuz_escalation(self, heuristic_classifier, hormuz_escalation_event):
        result = heuristic_classifier.classify(hormuz_escalation_event)
        assert result.event_type == "hormuz_escalation"
        assert result.actionable is True

    def test_fed_dovish_caught(self, heuristic_classifier, fed_dovish_event):
        result = heuristic_classifier.classify(fed_dovish_event)
        assert result.event_type == "fed_dovish_signal"
        assert result.actionable is True

    def test_banned_source_rejected(self, heuristic_classifier, banned_source_event):
        result = heuristic_classifier.classify(banned_source_event)
        assert result.actionable is False
        assert result.event_type is None
        assert "banned" in result.reasoning.lower()

    def test_stablecoin_depeg_caught(self, heuristic_classifier, stable_depeg_event):
        result = heuristic_classifier.classify(stable_depeg_event)
        assert result.event_type == "stablecoin_depeg"
        assert result.actionable is True

    def test_generic_noise_no_signal(self, heuristic_classifier, noise_event):
        result = heuristic_classifier.classify(noise_event)
        assert result.actionable is False
        assert result.event_type is None


# ---------------------------------------------------------------------------
# Tests — mapping resolution
# ---------------------------------------------------------------------------

class TestAssetMapping:

    def test_hormuz_easing_triggers_have_no_excluded(self):
        triggers = get_triggers_for_event("hormuz_easing")
        for t in triggers:
            assert t.ticker not in EXCLUDED_TICKERS, \
                f"Excluded ticker {t.ticker} appeared in triggers"

    def test_all_events_have_triggers(self):
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            assert len(triggers) > 0, f"Event {event_type} has no triggers"

    def test_unknown_event_returns_empty(self):
        assert get_triggers_for_event("nonexistent_event") == []

    def test_size_scaling_by_bucket_cap(self):
        triggers_full = get_triggers_for_event("hormuz_easing", bucket_cap_pct=100.0)
        triggers_half = get_triggers_for_event("hormuz_easing", bucket_cap_pct=50.0)
        assert len(triggers_full) == len(triggers_half)
        for full, half in zip(triggers_full, triggers_half):
            # Half cap should give half size
            assert half.suggested_size_pct_bucket == pytest.approx(
                full.suggested_size_pct_bucket / 2, rel=1e-3,
            )

    def test_no_btc_in_any_mapping(self):
        """BTC is excluded per QA protocol — must never appear in triggers."""
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert "BTC" not in t.ticker.upper(), \
                    f"BTC in {event_type}: {t.ticker}"

    def test_supported_events_count(self):
        """Smoke check: have at least 25 mapped events."""
        events = list_supported_events()
        assert len(events) >= 25, f"Only {len(events)} events mapped"

    def test_is_event_supported(self):
        assert is_event_supported("hormuz_easing")
        assert not is_event_supported("definitely_not_an_event")
