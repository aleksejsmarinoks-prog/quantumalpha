"""
QA Trade Trigger — Backtest Harness
=====================================

Replays historical events through the pipeline. Used for:
  - Verifying that classifier still catches old known events after rule changes
  - Measuring false-positive rate on known noise
  - Calibrating thresholds before deploying

Phase 4 scope: minimal — runs against hardcoded historical events.
Phase 5 will fetch real Polymarket history + price data.

Usage:
    python -m bot.trade_trigger.backtest

Or programmatically:
    results = await backtest_known_events(db, classifier)
    print(format_backtest_report(results))

Author: QuantumAlpha
Version: 0.4.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, TYPE_CHECKING

from .models import NewsEvent, Tier
from .pipeline import PipelineOrchestrator, PipelineConfig, PipelineDecision
from .classifier import TradeTriggerClassifier
from .filters.velocity_tracker import VelocityTracker
from .filters.corroboration_gate import CorroborationGate

if TYPE_CHECKING:
    from .db import TradeTriggerDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Historical event corpus
# ---------------------------------------------------------------------------

@dataclass
class BacktestCase:
    """One historical event with expected pipeline outcome."""
    name: str
    event: NewsEvent
    expected_event_type: Optional[str]   # what classifier should produce
    expected_to_fire: bool                # whether pipeline should ultimately fire
    notes: str = ""


def _make_event(
    headline: str, body: str, domain: str, tier: Tier, ts: datetime,
    raw_id: str, url: Optional[str] = None,
) -> NewsEvent:
    return NewsEvent(
        headline=headline, body=body,
        source_url=url or f"https://{domain}/post/{raw_id}",
        source_domain=domain, source_tier=tier,
        published_at=ts, fetched_at=ts, raw_id=raw_id,
    )


def historical_corpus() -> List[BacktestCase]:
    """Curated set of real historical events for regression testing."""
    cases: List[BacktestCase] = []

    # The May 3 2026 Hormuz event — the one we missed
    cases.append(BacktestCase(
        name="hormuz_easing_may3_2026",
        event=_make_event(
            headline="Trump: U.S. will guide stranded ships through Strait of Hormuz",
            body=(
                "President Trump said the United States will guide stranded "
                "ships through the Strait of Hormuz, signaling a major shift "
                "in U.S.-Iran diplomatic posture. Discussions described as "
                "very positive."
            ),
            domain="truthsocial.com",
            tier=Tier.T1,
            ts=datetime(2026, 5, 3, 22, 0, tzinfo=timezone.utc),
            raw_id="hist_hormuz_easing_20260503",
        ),
        expected_event_type="hormuz_easing",
        expected_to_fire=True,
        notes="Critical regression — this is what we missed live",
    ))

    # Synthetic but realistic: USDC depeg
    cases.append(BacktestCase(
        name="usdc_depeg_hypothetical",
        event=_make_event(
            headline="USDC loses peg, trades at $0.92 amid Circle reserve concerns",
            body=(
                "USDC, the second-largest stablecoin, broke its dollar peg "
                "Wednesday morning, falling to $0.92 amid concerns about Circle's "
                "reserve composition."
            ),
            domain="bloomberg.com",
            tier=Tier.T1,
            ts=datetime(2026, 5, 6, 9, 30, tzinfo=timezone.utc),
            raw_id="hist_usdc_depeg",
        ),
        expected_event_type="stablecoin_depeg",
        expected_to_fire=False,  # single-source bloomberg → corroboration fail
        notes="Single non-direct source — corroboration gate should reject",
    ))

    # Fed dovish signal (direct source)
    cases.append(BacktestCase(
        name="fed_dovish_warsh",
        event=_make_event(
            headline="Warsh signals aggressive rate cuts in first speech as nominee",
            body=(
                "Kevin Warsh said the Fed should cut rates faster. He called "
                "the 2022 inflation surge the Fed's worst mistake in 40 years."
            ),
            domain="federalreserve.gov",
            tier=Tier.T1,
            ts=datetime(2026, 5, 6, 14, 0, tzinfo=timezone.utc),
            raw_id="hist_warsh_dovish",
        ),
        expected_event_type="fed_dovish_signal",
        expected_to_fire=True,
        notes="Direct authoritative source — bypasses corroboration",
    ))

    # Generic noise — should not fire
    cases.append(BacktestCase(
        name="generic_earnings_noise",
        event=_make_event(
            headline="Tech earnings season kicks off Tuesday",
            body="Several large-cap tech companies will report earnings this week.",
            domain="cnbc.com",
            tier=Tier.T1,
            ts=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
            raw_id="hist_earnings_noise",
        ),
        expected_event_type=None,
        expected_to_fire=False,
        notes="Should be rejected by classifier (no matching keywords)",
    ))

    # Banned source — must reject
    cases.append(BacktestCase(
        name="banned_source_rejection",
        event=_make_event(
            headline="Hormuz strike imminent, sources say",
            body="Anonymous sources reported imminent action.",
            domain="news24.com",
            tier=Tier.BANNED,
            ts=datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc),
            raw_id="hist_banned",
        ),
        expected_event_type=None,
        expected_to_fire=False,
        notes="Banned tier — must reject regardless of headline",
    ))

    return cases


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    case_name: str
    expected_event_type: Optional[str]
    actual_event_type: Optional[str]
    expected_fire: bool
    actual_fire: bool
    score: float
    rejection_stage: Optional[str]
    classifier_match: bool
    fire_match: bool
    notes: str


@dataclass
class BacktestReport:
    total: int
    classifier_correct: int
    fire_decisions_correct: int
    results: List[BacktestResult] = field(default_factory=list)

    @property
    def classifier_accuracy(self) -> float:
        return self.classifier_correct / self.total if self.total else 0.0

    @property
    def fire_accuracy(self) -> float:
        return self.fire_decisions_correct / self.total if self.total else 0.0

    def to_text(self) -> str:
        lines = [
            "BACKTEST REPORT",
            "─" * 50,
            f"Total cases:           {self.total}",
            f"Classifier accuracy:   {self.classifier_correct}/{self.total} "
            f"({self.classifier_accuracy * 100:.0f}%)",
            f"Fire-decision accuracy: {self.fire_decisions_correct}/{self.total} "
            f"({self.fire_accuracy * 100:.0f}%)",
            "",
            "Per-case detail:",
        ]
        for r in self.results:
            cls_mark = "✓" if r.classifier_match else "✗"
            fire_mark = "✓" if r.fire_match else "✗"
            lines.append(
                f"  [{cls_mark} cls / {fire_mark} fire] {r.case_name}"
            )
            lines.append(
                f"      cls: expected={r.expected_event_type} got={r.actual_event_type}"
            )
            lines.append(
                f"      fire: expected={r.expected_fire} got={r.actual_fire} "
                f"(stage: {r.rejection_stage or 'fired'})"
            )
            if r.notes:
                lines.append(f"      note: {r.notes}")
        return "\n".join(lines)


async def run_backtest(
    db: "TradeTriggerDB",
    cases: Optional[List[BacktestCase]] = None,
    enable_l2: bool = False,
    min_score: float = 5.0,
) -> BacktestReport:
    """Run all cases through a fresh pipeline. Returns BacktestReport.

    Each case gets a fresh-ish pipeline state. We use the supplied DB but
    cases use unique raw_ids so dedup doesn't interfere across runs.
    """
    cases = cases or historical_corpus()

    classifier = TradeTriggerClassifier(enable_l2=enable_l2)
    pipeline = PipelineOrchestrator(
        db=db,
        classifier=classifier,
        velocity=VelocityTracker(),
        corroboration=CorroborationGate(db),
        anti_bias=None,                      # backtest skips anti-bias by design
        config=PipelineConfig(
            min_actionability_score=min_score,
            require_anti_bias=False,
        ),
    )

    results: List[BacktestResult] = []
    classifier_correct = 0
    fire_correct = 0

    for case in cases:
        # Use case event's published_at as 'now' so velocity gate doesn't reject
        # on age. We're doing capability check, not real-time.
        case_now = case.event.published_at + timedelta(minutes=1)

        decision = await pipeline.process_event(case.event, now=case_now)

        # Extract classifier output from audit trail
        cls_step = next(
            (s for s in decision.audit_trail if s.get("step") == "classifier"),
            None,
        )
        actual_event_type = cls_step.get("event_type") if cls_step else None
        score = cls_step.get("score", 0.0) if cls_step else 0.0

        cls_match = actual_event_type == case.expected_event_type
        fire_match = decision.fired == case.expected_to_fire

        if cls_match:
            classifier_correct += 1
        if fire_match:
            fire_correct += 1

        results.append(BacktestResult(
            case_name=case.name,
            expected_event_type=case.expected_event_type,
            actual_event_type=actual_event_type,
            expected_fire=case.expected_to_fire,
            actual_fire=decision.fired,
            score=score,
            rejection_stage=decision.rejection_stage,
            classifier_match=cls_match,
            fire_match=fire_match,
            notes=case.notes,
        ))

    return BacktestReport(
        total=len(cases),
        classifier_correct=classifier_correct,
        fire_decisions_correct=fire_correct,
        results=results,
    )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

async def _cli_main():
    """Standalone runner: python -m bot.trade_trigger.backtest"""
    import asyncio
    import os
    import tempfile
    from .db import TradeTriggerDB

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db_path = os.getenv("TT_BACKTEST_DB")
    if db_path:
        db = TradeTriggerDB(db_path)
    else:
        # Use a temp DB so we don't pollute production
        tmpdir = tempfile.mkdtemp(prefix="tt_backtest_")
        db = TradeTriggerDB(f"{tmpdir}/backtest.db")
        print(f"Using temp DB: {tmpdir}/backtest.db")

    report = await run_backtest(db)
    print(report.to_text())


if __name__ == "__main__":
    import asyncio
    asyncio.run(_cli_main())
