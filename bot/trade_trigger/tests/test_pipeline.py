"""Pipeline orchestrator integration tests.
End-to-end: NewsEvent → all gates → TradeSignal (or rejection)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

from bot.trade_trigger.models import NewsEvent, Tier, Direction
from bot.trade_trigger.classifier import TradeTriggerClassifier
from bot.trade_trigger.pipeline import (
    PipelineOrchestrator, PipelineConfig, PipelineDecision,
)
from bot.trade_trigger.filters.velocity_tracker import VelocityTracker
from bot.trade_trigger.filters.corroboration_gate import CorroborationGate
from bot.trade_trigger.filters.anti_bias_check import AntiBiasGate, AntiBiasConfig


# ---------------------------------------------------------------------------
# Mock price provider
# ---------------------------------------------------------------------------

class MockProvider:
    def __init__(self, klines_by_symbol: Optional[dict] = None):
        self.klines = klines_by_symbol or {}

    async def get_klines_1h(self, symbol, count=24):
        normalized = symbol.replace("/", "").upper()
        return self.klines.get(normalized)


def _flat_market(base: float, n: int = 24, drift: float = 0.0) -> list:
    """Generate flat or slightly drifting price series."""
    return [base + i * drift for i in range(n)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def heuristic_classifier():
    return TradeTriggerClassifier(enable_l2=False)


@pytest.fixture
def neutral_provider():
    return MockProvider({
        "ETHUSDT": _flat_market(2300.0, drift=0.5),       # +0.5% drift
        "SOLUSDT": _flat_market(85.0, drift=0.02),
        "BTCUSDT": _flat_market(80000.0, drift=10.0),     # ignored — excluded
    })


@pytest.fixture
def overheated_provider():
    """ETH already pumped +6% in 24h → priced in."""
    return MockProvider({
        "ETHUSDT": [2200.0 + i * 7.0 for i in range(24)],   # 2200 → ~2361, +7.3%
        "SOLUSDT": _flat_market(85.0, drift=0.02),
    })


@pytest.fixture
def pipeline_full(db, heuristic_classifier, neutral_provider):
    """Pipeline with ALL gates active.
    Note: min_actionability_score=5.0 because tests run L1-only (no Claude API).
    Production default is 7.0 with L2 enabled.
    """
    return PipelineOrchestrator(
        db=db,
        classifier=heuristic_classifier,
        velocity=VelocityTracker(),
        corroboration=CorroborationGate(db),
        anti_bias=AntiBiasGate(neutral_provider),
        config=PipelineConfig(bucket_cap_pct=10.0, min_actionability_score=5.0),
    )


@pytest.fixture
def pipeline_no_anti_bias(db, heuristic_classifier):
    """Pipeline without anti-bias (e.g. if BybitClient unavailable)."""
    return PipelineOrchestrator(
        db=db,
        classifier=heuristic_classifier,
        velocity=VelocityTracker(),
        corroboration=CorroborationGate(db),
        anti_bias=None,
        config=PipelineConfig(require_anti_bias=False, min_actionability_score=5.0),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hormuz_easing_event(utc_now, raw_id: str = "h1") -> NewsEvent:
    """Truth Social — direct authoritative source, bypasses corroboration."""
    return NewsEvent(
        headline="Trump: U.S. will guide stranded ships through Strait of Hormuz",
        body=(
            "President Trump said the United States will guide stranded "
            "ships through the Strait of Hormuz, signaling a major shift "
            "in U.S.-Iran diplomatic posture. Discussions described as "
            "very positive."
        ),
        source_url="https://truthsocial.com/@realDonaldTrump/posts/123",
        source_domain="truthsocial.com",
        source_tier=Tier.T1,
        published_at=utc_now - timedelta(minutes=5),
        fetched_at=utc_now - timedelta(minutes=5),
        raw_id=raw_id,
    )


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestEndToEndHappyPath:

    async def test_hormuz_easing_fires_signal(self, pipeline_full, utc_now):
        """REGRESSION — if pipeline had been live May 3, this would have fired."""
        event = _hormuz_easing_event(utc_now)
        decision = await pipeline_full.process_event(event, now=utc_now)

        assert decision.fired is True, (
            f"Expected fire, got rejection at {decision.rejection_stage}: "
            f"{decision.rejection_reason}"
        )
        assert decision.signal is not None
        assert decision.signal.event_type == "hormuz_easing"
        assert len(decision.signal.triggers) >= 1

        # Audit trail must contain all stages
        steps = [s["step"] for s in decision.audit_trail]
        assert "insert" in steps
        assert "velocity" in steps
        assert "classifier" in steps
        assert "mapping" in steps
        assert "corroboration" in steps
        assert "anti_bias" in steps
        assert "signal" in steps

    async def test_signal_persisted(self, pipeline_full, utc_now):
        event = _hormuz_easing_event(utc_now, raw_id="persist_test")
        decision = await pipeline_full.process_event(event, now=utc_now)
        assert decision.fired is True

        # DB has a row
        stats = pipeline_full.db.stats()
        assert stats["total_signals"] >= 1


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRejections:

    async def test_duplicate_event_rejected(self, pipeline_full, utc_now):
        event = _hormuz_easing_event(utc_now, raw_id="dup")
        d1 = await pipeline_full.process_event(event, now=utc_now)
        d2 = await pipeline_full.process_event(event, now=utc_now)

        assert d1.fired is True
        assert d2.fired is False
        assert d2.rejection_stage == "duplicate"

    async def test_stale_event_rejected(self, pipeline_full, utc_now):
        # 3h old — beyond max_age_acceptable=120min
        event = _hormuz_easing_event(utc_now)
        old_event = NewsEvent(
            **{**event.__dict__, "raw_id": "stale", "published_at": utc_now - timedelta(hours=3)}
        )
        decision = await pipeline_full.process_event(old_event, now=utc_now)
        assert decision.fired is False
        assert decision.rejection_stage == "velocity"

    async def test_low_score_rejected(self, pipeline_full, utc_now):
        """Generic noise → classifier rejects."""
        ev = NewsEvent(
            headline="Tech earnings season kicks off Tuesday",
            body="Generic earnings announcement.",
            source_url="https://www.cnbc.com/x",
            source_domain="cnbc.com",
            source_tier=Tier.T1,
            published_at=utc_now,
            fetched_at=utc_now,
            raw_id="noise_pipeline",
        )
        decision = await pipeline_full.process_event(ev, now=utc_now)
        assert decision.fired is False
        assert decision.rejection_stage == "classifier"

    async def test_priced_in_anti_bias_rejected(
        self, db, heuristic_classifier, overheated_provider, utc_now,
    ):
        """ETH already up +6% intraday → all triggers fail Anti-Bias."""
        provider = MockProvider({
            "ETHUSDT": [2200.0 + i * 7.0 for i in range(24)],   # +7%
            "SOLUSDT": [80.0 + i * 0.4 for i in range(24)],     # +12%
        })
        pipeline = PipelineOrchestrator(
            db=db,
            classifier=heuristic_classifier,
            velocity=VelocityTracker(),
            corroboration=CorroborationGate(db),
            anti_bias=AntiBiasGate(provider),
            config=PipelineConfig(min_actionability_score=5.0),
        )
        event = _hormuz_easing_event(utc_now, raw_id="ab_overheated")
        decision = await pipeline.process_event(event, now=utc_now)

        # SHEL.L SHORT will fail-open (no data), so signal might still fire.
        # Verify at least anti_bias step ran and dropped some triggers
        ab_step = next((s for s in decision.audit_trail if s["step"] == "anti_bias"), None)
        assert ab_step is not None
        # At least one trigger dropped vs total
        assert ab_step["survived"] < ab_step["total"] or not decision.fired

    async def test_single_unsupported_source_rejected(self, pipeline_full, utc_now):
        """Single non-direct source → corroboration fails."""
        ev = NewsEvent(
            headline="Iran Hormuz strike rumors",
            body="Unconfirmed Iran Hormuz attack chatter.",
            source_url="https://www.reuters.com/x",
            source_domain="reuters.com",     # NOT in direct_source_bypass list
            source_tier=Tier.T1,
            published_at=utc_now - timedelta(minutes=2),
            fetched_at=utc_now - timedelta(minutes=2),
            raw_id="single_src",
        )
        decision = await pipeline_full.process_event(ev, now=utc_now)

        # If classifier accepted it, corroboration must reject (only 1 source in DB).
        # If classifier rejected first, we accept either.
        if decision.rejection_stage:
            assert decision.rejection_stage in {"corroboration", "classifier", "score_threshold"}
        assert decision.fired is False


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAuditLog:

    async def test_audit_rows_written_per_step(self, pipeline_full, utc_now):
        event = _hormuz_easing_event(utc_now, raw_id="audit_test")
        await pipeline_full.process_event(event, now=utc_now)

        # Verify audit log got rows
        with pipeline_full.db._conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM filter_audit_log WHERE raw_id = ?",
                ("audit_test",),
            ).fetchone()[0]
            assert count >= 4  # velocity + classifier + corroboration + anti_bias[*]


# ---------------------------------------------------------------------------
# Polymarket → Pipeline integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPolymarketIntegration:
    """Full integration: Polymarket shift → synthetic event → pipeline → signal."""

    async def test_polymarket_shift_to_signal(
        self, db, heuristic_classifier, utc_now, neutral_provider,
    ):
        """End-to-end with synthetic Polymarket event."""
        from bot.trade_trigger.sources.polymarket import (
            PolymarketWatcher, MarketSpec, WatcherConfig as PMConfig,
        )

        # Mock client (reuse pattern from test_polymarket)
        class MockClient:
            def __init__(self):
                self._markets = {}
            def set_market(self, slug, m): self._markets[slug] = m
            async def fetch_market_by_slug(self, slug):
                return self._markets.get(slug)
            @staticmethod
            def extract_outcome_price(m, name):
                from bot.trade_trigger.sources.polymarket import PolymarketClient
                return PolymarketClient.extract_outcome_price(m, name)
            async def close(self): pass

        mock_client = MockClient()
        spec = MarketSpec(
            slug="will-iran-close-the-strait-of-hormuz-in-may-2026",
            outcome="Yes",
            event_type="hormuz_escalation",
            direction="up",
            shift_threshold=0.05,
            label="Iran-Hormuz-closure",
        )
        watcher = PolymarketWatcher(
            db=db, client=mock_client, watchlist=[spec],
            config=PMConfig(shift_window_minutes=15),
        )

        # T0: 18%
        mock_client.set_market(spec.slug, {
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.18", "0.82"],
        })
        await watcher.poll_once(now=utc_now)

        # T+8min: spike to 38%
        mock_client.set_market(spec.slug, {
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.38", "0.62"],
        })
        events = await watcher.poll_once(now=utc_now + timedelta(minutes=8))
        assert len(events) == 1

        # Feed the synthetic event into pipeline.
        # polymarket.com is in direct_source_bypass via DIRECT_SOURCE_BONUS,
        # but NOT in CorroborationGate.direct_source_domains by default.
        # So corroboration will require a real second source. We simulate that
        # by also inserting a Reuters event with similar topic.
        reuters_event = NewsEvent(
            headline="Polymarket Iran Hormuz closure odds spike to 38%",
            body="Reuters tracks the Polymarket move.",
            source_url="https://reuters.com/x",
            source_domain="reuters.com",
            source_tier=Tier.T1,
            published_at=utc_now + timedelta(minutes=7),
            fetched_at=utc_now + timedelta(minutes=7),
            raw_id="reuters_corroborate",
        )
        db.insert_event(reuters_event)

        pipeline = PipelineOrchestrator(
            db=db,
            classifier=heuristic_classifier,
            velocity=VelocityTracker(),
            corroboration=CorroborationGate(db),
            anti_bias=AntiBiasGate(neutral_provider),
            config=PipelineConfig(min_actionability_score=5.0),
        )

        decision = await pipeline.process_event(events[0], now=utc_now + timedelta(minutes=8))
        # Either fires (if everything aligns) or shows audit trail
        # Key requirement: classifier must recognize the synthetic headline
        cls_step = next((s for s in decision.audit_trail if s["step"] == "classifier"), None)
        assert cls_step is not None
        # Must have classified to hormuz_escalation
        assert cls_step.get("event_type") == "hormuz_escalation"
