"""Filter tests — velocity, corroboration, anti-bias."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from bot.trade_trigger.models import NewsEvent, Tier, Direction, AssetTrigger
from bot.trade_trigger.filters.velocity_tracker import (
    VelocityTracker, VelocityConfig,
)
from bot.trade_trigger.filters.corroboration_gate import (
    CorroborationGate, CorroborationConfig, extract_topic_keywords,
)
from bot.trade_trigger.filters.anti_bias_check import (
    AntiBiasGate, AntiBiasConfig, compute_rsi,
)


# ---------------------------------------------------------------------------
# Velocity tests
# ---------------------------------------------------------------------------

class TestVelocityTracker:

    def test_fresh_event_passes(self, event_factory, utc_now):
        ev = event_factory(published_at=utc_now - timedelta(minutes=5))
        tracker = VelocityTracker()
        passed, status, age = tracker.check(ev, now=utc_now)
        assert passed is True
        assert status == "fresh"
        assert age == pytest.approx(5.0, abs=0.1)

    def test_decaying_event_passes_with_status(self, event_factory, utc_now):
        ev = event_factory(published_at=utc_now - timedelta(minutes=60))
        tracker = VelocityTracker()
        passed, status, _ = tracker.check(ev, now=utc_now)
        assert passed is True
        assert status == "decaying"

    def test_stale_event_rejected(self, event_factory, utc_now):
        ev = event_factory(published_at=utc_now - timedelta(hours=3))
        tracker = VelocityTracker()
        passed, status, _ = tracker.check(ev, now=utc_now)
        assert passed is False
        assert status == "stale"

    def test_decay_factor_linear(self, event_factory, utc_now):
        # Fresh
        ev_fresh = event_factory(published_at=utc_now - timedelta(minutes=10))
        # Mid (between 30 and 120)
        ev_mid = event_factory(published_at=utc_now - timedelta(minutes=75))
        # Stale
        ev_stale = event_factory(published_at=utc_now - timedelta(hours=3))

        t = VelocityTracker()
        assert t.conviction_decay_factor(ev_fresh, now=utc_now) == 1.0
        assert 0.0 < t.conviction_decay_factor(ev_mid, now=utc_now) < 1.0
        assert t.conviction_decay_factor(ev_stale, now=utc_now) == 0.0

    def test_negative_age_treated_as_fresh(self, event_factory, utc_now):
        """Clock skew protection — future timestamp passes but logs warning."""
        ev = event_factory(published_at=utc_now + timedelta(minutes=5))
        tracker = VelocityTracker()
        passed, status, _ = tracker.check(ev, now=utc_now)
        assert passed is True
        assert status == "fresh"


# ---------------------------------------------------------------------------
# Topic extraction
# ---------------------------------------------------------------------------

class TestTopicExtraction:

    def test_extracts_proper_nouns(self):
        kws = extract_topic_keywords("Trump: U.S. will guide ships through Hormuz")
        assert "trump" in kws
        assert "hormuz" in kws

    def test_extracts_verbs(self):
        kws = extract_topic_keywords("Iran missile strike on shipping")
        assert "iran" in kws
        assert "strike" in kws

    def test_max_keywords_respected(self):
        kws = extract_topic_keywords(
            "Trump and Biden discuss Iran Russia China and Bitcoin",
            max_keywords=3,
        )
        assert len(kws) <= 3

    def test_empty_for_generic_text(self):
        kws = extract_topic_keywords("The quarterly meeting was productive.")
        assert kws == []


# ---------------------------------------------------------------------------
# Corroboration gate
# ---------------------------------------------------------------------------

class TestCorroborationGate:

    def test_direct_source_bypass_truth_social(self, db, event_factory):
        ev = event_factory(
            headline="Trump confirms Hormuz transit deal",
            source_domain="truthsocial.com",
            source_tier=Tier.T1,
        )
        db.insert_event(ev)
        gate = CorroborationGate(db)
        result = gate.check(ev)
        assert result.passed is True
        assert result.bypassed_direct_source is True

    def test_direct_source_bypass_whitehouse(self, db, event_factory):
        ev = event_factory(
            headline="White House announces Iran deal",
            source_domain="whitehouse.gov",
            source_tier=Tier.T1,
        )
        db.insert_event(ev)
        gate = CorroborationGate(db)
        result = gate.check(ev)
        assert result.passed is True

    def test_single_non_direct_source_fails(self, db, event_factory):
        ev = event_factory(
            headline="Iran Hormuz strike rumors",
            source_domain="reuters.com",
            source_tier=Tier.T1,
        )
        db.insert_event(ev)
        gate = CorroborationGate(db)
        result = gate.check(ev)
        # Only 1 distinct source — should fail (need ≥2)
        assert result.passed is False
        assert result.distinct_sources == 1

    def test_two_tier1_sources_pass(self, db, event_factory, utc_now):
        # Reuters publishes first
        ev1 = event_factory(
            headline="Iran strikes Hormuz tanker",
            source_domain="reuters.com",
            source_tier=Tier.T1,
            published_at=utc_now - timedelta(minutes=5),
            raw_id="ev1",
        )
        # Bloomberg confirms 3 min later
        ev2 = event_factory(
            headline="Iran strikes Hormuz oil tanker, Bloomberg confirms",
            source_domain="bloomberg.com",
            source_tier=Tier.T1,
            published_at=utc_now - timedelta(minutes=2),
            raw_id="ev2",
        )
        db.insert_event(ev1)
        db.insert_event(ev2)

        gate = CorroborationGate(db)
        result = gate.check(ev2, now=utc_now)
        assert result.passed is True
        assert result.distinct_sources >= 2
        assert result.tier1_match_count >= 2


# ---------------------------------------------------------------------------
# RSI computation
# ---------------------------------------------------------------------------

class TestRSI:

    def test_insufficient_data_returns_none(self):
        assert compute_rsi([100, 101, 102]) is None

    def test_constant_prices_rsi_is_50_or_undefined(self):
        # All gains zero, all losses zero → avg_loss=0 → returns 100.0 (per impl)
        prices = [100.0] * 20
        rsi = compute_rsi(prices)
        # Edge case: division by zero handled, returns 100
        assert rsi == 100.0

    def test_uptrend_high_rsi(self):
        prices = [100 + i for i in range(20)]  # monotonic up
        rsi = compute_rsi(prices)
        assert rsi == 100.0  # all gains, no losses

    def test_downtrend_low_rsi(self):
        prices = [100 - i for i in range(20)]
        rsi = compute_rsi(prices)
        assert rsi is not None
        assert rsi < 30.0  # heavy oversold

    def test_mixed_realistic(self):
        # Slightly uptrending mixed
        prices = [100, 102, 101, 103, 104, 102, 105, 106, 105, 107,
                  108, 107, 109, 110, 109, 111, 112, 113, 112, 114]
        rsi = compute_rsi(prices)
        assert rsi is not None
        assert 50.0 < rsi < 95.0


# ---------------------------------------------------------------------------
# Anti-Bias Gate (async)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAntiBiasGate:

    async def test_clear_runway_passes(self, mock_price_provider):
        # Sideways prices, neutral RSI
        provider = mock_price_provider({
            "ETHUSDT": [2000.0, 2010.0, 2005.0, 2015.0, 2010.0,
                        2020.0, 2015.0, 2025.0, 2020.0, 2030.0,
                        2025.0, 2035.0, 2030.0, 2040.0, 2035.0,
                        2045.0, 2040.0, 2050.0, 2045.0, 2055.0,
                        2050.0, 2060.0, 2055.0, 2065.0],
        })
        trigger = AssetTrigger(
            ticker="ETH/USDT", venue="Bybit",
            direction=Direction.LONG, conviction=0.7,
            suggested_size_pct_bucket=7.0,
        )
        gate = AntiBiasGate(provider)
        result = await gate.check_trigger(trigger)
        # Total move 100→103 = +3% < 5% threshold, pass
        assert result.passed is True

    async def test_priced_in_long_skipped(self, mock_price_provider):
        # 24h up >5% — already pumped
        provider = mock_price_provider({
            "ETHUSDT": [2000.0 + i * 5 for i in range(24)],  # +60 = +3% wait, recompute
        })
        # Actually that's 2000 → 2115, +5.75% — exceeds threshold
        trigger = AssetTrigger(
            ticker="ETH/USDT", venue="Bybit",
            direction=Direction.LONG, conviction=0.7,
            suggested_size_pct_bucket=7.0,
        )
        gate = AntiBiasGate(provider)
        result = await gate.check_trigger(trigger)
        assert result.passed is False
        assert result.verdict == "skip_priced_in"
        assert result.intraday_change_pct >= 5.0

    async def test_no_data_fails_open(self, mock_price_provider):
        provider = mock_price_provider({})  # no data
        trigger = AssetTrigger(
            ticker="ETH/USDT", venue="Bybit",
            direction=Direction.LONG, conviction=0.7,
            suggested_size_pct_bucket=7.0,
        )
        gate = AntiBiasGate(provider)
        result = await gate.check_trigger(trigger)
        # Fail open: pass with multiplier 1.0 but flag in reason
        assert result.passed is True
        assert "skipped" in result.reason.lower() or "no live" in result.reason.lower()
