"""
QA Trade Trigger — Main Pipeline Orchestrator
==============================================

Connects all components in proper order. This is the brain.

Flow per NewsEvent:

    1. Insert/dedup in DB           ──→ skip if duplicate
    2. Velocity gate                ──→ skip if stale (>2h)
    3. Classifier (L1+L2)           ──→ skip if not actionable
    4. Mapping resolution           ──→ skip if event_type unmapped
    5. Corroboration gate           ──→ skip if single-source rumor
    6. Anti-Bias gate (per trigger) ──→ filter triggers, possibly downgrade size
    7. Build TradeSignal            ──→ store + push to Telegram

Each step writes to filter_audit_log for postmortem analysis.

Public API:
    pipeline = PipelineOrchestrator(db, classifier, ...)
    signal = await pipeline.process_event(event)
    if signal:
        await bot.send_alert(signal)

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from .models import (
    NewsEvent, TradeSignal, AssetTrigger, ClassificationResult,
    Direction, Tier, TriggerVerdict,
)
from .classifier import TradeTriggerClassifier, L2_THRESHOLD
from .trade_trigger_mapping import (
    get_triggers_for_event, is_event_supported, get_max_half_life,
)
from .filters.velocity_tracker import VelocityTracker, VelocityConfig
from .filters.corroboration_gate import CorroborationGate, CorroborationConfig
from .filters.anti_bias_check import AntiBiasGate, AntiBiasConfig

if TYPE_CHECKING:
    from .db import TradeTriggerDB
    from .filters.anti_bias_check import LivePriceProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline result envelope
# ---------------------------------------------------------------------------

@dataclass
class PipelineDecision:
    """Trace of every decision in the pipeline. Useful for /tt_audit and tests."""
    raw_id: str
    fired: bool                          # True if signal was emitted
    signal: Optional[TradeSignal] = None
    rejection_stage: Optional[str] = None  # 'duplicate' / 'velocity' / 'classifier' / etc.
    rejection_reason: Optional[str] = None
    audit_trail: List[dict] = field(default_factory=list)  # ordered list of step results

    def add_step(self, name: str, passed: bool, **details) -> None:
        self.audit_trail.append({
            "step": name,
            "passed": passed,
            **details,
        })


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    bucket_cap_pct: float = 10.0          # default size = conviction × 10%
    min_actionability_score: float = L2_THRESHOLD   # 7.0
    require_anti_bias: bool = True        # if False, skip live RSI/price check
    require_corroboration: bool = True
    require_velocity: bool = True
    log_audit_to_db: bool = True


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

class PipelineOrchestrator:
    """Glue between source events and trade signals.

    Components are passed via DI for testability and runtime flexibility:
      - classifier:    TradeTriggerClassifier (L1+L2)
      - velocity:      VelocityTracker
      - corroboration: CorroborationGate (None = skip)
      - anti_bias:     AntiBiasGate (None = skip live price check)
    """

    def __init__(
        self,
        db: "TradeTriggerDB",
        classifier: TradeTriggerClassifier,
        velocity: Optional[VelocityTracker] = None,
        corroboration: Optional[CorroborationGate] = None,
        anti_bias: Optional[AntiBiasGate] = None,
        config: Optional[PipelineConfig] = None,
    ):
        self.db = db
        self.classifier = classifier
        self.velocity = velocity or VelocityTracker()
        self.corroboration = corroboration   # None means gate disabled
        self.anti_bias = anti_bias           # None means gate disabled
        self.config = config or PipelineConfig()

    # -----------------------------------------------------------------------
    # Main entry
    # -----------------------------------------------------------------------

    async def process_event(
        self, event: NewsEvent, now: Optional[datetime] = None,
    ) -> PipelineDecision:
        """Run event through the full pipeline. Returns PipelineDecision."""
        now = now or datetime.now(timezone.utc)
        decision = PipelineDecision(raw_id=event.raw_id, fired=False)

        # Step 1: dedup insert
        is_new = self.db.insert_event(event)
        decision.add_step("insert", passed=is_new, headline=event.headline[:80])
        if not is_new:
            decision.rejection_stage = "duplicate"
            decision.rejection_reason = "Event already in DB (dedup)"
            self._log_audit(event.raw_id, "insert", False, decision.rejection_reason)
            return decision

        # Step 2: velocity
        if self.config.require_velocity:
            v_passed, v_status, v_age = self.velocity.check(event, now=now)
            decision.add_step(
                "velocity", passed=v_passed,
                age_min=round(v_age, 1), status=v_status,
            )
            self._log_audit(
                event.raw_id, "velocity", v_passed,
                f"age={v_age:.1f}min status={v_status}",
                metadata={"age_minutes": v_age, "status": v_status},
            )
            if not v_passed:
                decision.rejection_stage = "velocity"
                decision.rejection_reason = f"Stale: age={v_age:.1f}min"
                return decision

        # Step 3: classifier
        cls_result = self.classifier.classify(event)
        self.db.upsert_classification(event.raw_id, cls_result)
        decision.add_step(
            "classifier",
            passed=cls_result.actionable,
            event_type=cls_result.event_type,
            score=cls_result.actionability_score,
            confidence=cls_result.confidence,
        )
        self._log_audit(
            event.raw_id, "classifier", cls_result.actionable,
            cls_result.reasoning[:200],
            metadata={
                "event_type": cls_result.event_type,
                "score": cls_result.actionability_score,
                "confidence": cls_result.confidence,
            },
        )
        if not cls_result.actionable:
            decision.rejection_stage = "classifier"
            decision.rejection_reason = f"score={cls_result.actionability_score:.2f}"
            return decision

        # Score must clear L2 threshold
        if cls_result.actionability_score < self.config.min_actionability_score:
            decision.add_step(
                "score_threshold", passed=False,
                score=cls_result.actionability_score,
                threshold=self.config.min_actionability_score,
            )
            self._log_audit(
                event.raw_id, "score_threshold", False,
                f"score={cls_result.actionability_score:.2f} < {self.config.min_actionability_score}",
            )
            decision.rejection_stage = "score_threshold"
            decision.rejection_reason = (
                f"L2 score {cls_result.actionability_score:.2f} < "
                f"{self.config.min_actionability_score}"
            )
            return decision

        # Step 4: mapping
        if not cls_result.event_type or not is_event_supported(cls_result.event_type):
            decision.add_step("mapping", passed=False, event_type=cls_result.event_type)
            self._log_audit(
                event.raw_id, "mapping", False,
                f"event_type={cls_result.event_type} not in mapping",
            )
            decision.rejection_stage = "mapping"
            decision.rejection_reason = "Event type not mapped to triggers"
            return decision

        raw_triggers = get_triggers_for_event(
            cls_result.event_type, bucket_cap_pct=self.config.bucket_cap_pct,
        )
        if not raw_triggers:
            decision.add_step("mapping", passed=False, event_type=cls_result.event_type)
            decision.rejection_stage = "mapping"
            decision.rejection_reason = "Mapping returned empty (all excluded?)"
            return decision

        decision.add_step(
            "mapping", passed=True,
            event_type=cls_result.event_type,
            trigger_count=len(raw_triggers),
        )

        # Step 5: corroboration
        if self.corroboration is not None and self.config.require_corroboration:
            corr_result = self.corroboration.check(event, now=now)
            decision.add_step(
                "corroboration", passed=corr_result.passed,
                distinct_sources=corr_result.distinct_sources,
                tier1_matches=corr_result.tier1_match_count,
                bypassed=corr_result.bypassed_direct_source,
            )
            self._log_audit(
                event.raw_id, "corroboration", corr_result.passed,
                corr_result.reason[:200],
                metadata={
                    "distinct_sources": corr_result.distinct_sources,
                    "tier1_match_count": corr_result.tier1_match_count,
                    "bypassed_direct": corr_result.bypassed_direct_source,
                },
            )
            if not corr_result.passed:
                decision.rejection_stage = "corroboration"
                decision.rejection_reason = corr_result.reason
                return decision

        # Step 6: anti-bias per trigger (filter + downgrade)
        final_triggers: List[AssetTrigger] = []
        ab_audit_per_trigger = []

        if self.anti_bias is not None and self.config.require_anti_bias:
            for trig in raw_triggers:
                ab_result = await self.anti_bias.check_trigger(trig)
                ab_audit_per_trigger.append({
                    "ticker": trig.ticker,
                    "verdict": ab_result.verdict,
                    "rsi": ab_result.rsi,
                    "intraday_change_pct": ab_result.intraday_change_pct,
                    "size_multiplier": ab_result.suggested_size_multiplier,
                })
                self._log_audit(
                    event.raw_id, f"anti_bias[{trig.ticker}]", ab_result.passed,
                    ab_result.reason[:200],
                    metadata={
                        "ticker": trig.ticker,
                        "verdict": ab_result.verdict,
                        "rsi": ab_result.rsi,
                        "intraday_change_pct": ab_result.intraday_change_pct,
                    },
                )

                if not ab_result.passed:
                    continue  # drop this trigger

                # Apply size multiplier
                adjusted = AssetTrigger(
                    ticker=trig.ticker,
                    venue=trig.venue,
                    direction=trig.direction,
                    conviction=trig.conviction * ab_result.suggested_size_multiplier,
                    suggested_size_pct_bucket=(
                        trig.suggested_size_pct_bucket * ab_result.suggested_size_multiplier
                    ),
                    invalidation_price=trig.invalidation_price,
                    invalidation_reason=trig.invalidation_reason,
                    half_life_minutes=trig.half_life_minutes,
                )
                final_triggers.append(adjusted)
        else:
            # No anti-bias check — pass all raw_triggers through
            final_triggers = list(raw_triggers)

        decision.add_step(
            "anti_bias", passed=len(final_triggers) > 0,
            survived=len(final_triggers), total=len(raw_triggers),
            per_trigger=ab_audit_per_trigger,
        )

        if not final_triggers:
            decision.rejection_stage = "anti_bias"
            decision.rejection_reason = "All triggers failed Anti-Bias Gate (priced in / overheated)"
            return decision

        # Step 7: build & store signal
        sources = self._collect_corroborating_sources(event)
        signal = TradeSignal(
            event_type=cls_result.event_type,
            triggers=final_triggers,
            sources=sources,
            first_seen_utc=event.published_at,
            actionability_score=cls_result.actionability_score,
            reasoning=self._build_reasoning(event, cls_result, final_triggers),
            verdict=TriggerVerdict.FIRE,
        )
        signal_id = self.db.insert_signal(event.raw_id, signal)
        decision.add_step("signal", passed=True, signal_id=signal_id)
        decision.fired = True
        decision.signal = signal

        logger.info(
            "TRIGGER FIRED: event=%s score=%.2f triggers=%d signal_id=%d",
            cls_result.event_type, cls_result.actionability_score,
            len(final_triggers), signal_id,
        )
        return decision

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _collect_corroborating_sources(self, event: NewsEvent) -> List[str]:
        """Collect URLs of events that corroborated this one (or just current)."""
        if self.corroboration is None:
            return [event.source_url]
        # Re-check to grab match IDs
        try:
            result = self.corroboration.check(event)
            urls = []
            for raw_id in result.matching_event_ids:
                ev = self.db.get_event(raw_id)
                if ev:
                    urls.append(ev.source_url)
            return urls or [event.source_url]
        except Exception:
            return [event.source_url]

    def _build_reasoning(
        self, event: NewsEvent, cls: ClassificationResult,
        triggers: List[AssetTrigger],
    ) -> str:
        return (
            f"Event '{cls.event_type}' classified at score {cls.actionability_score:.2f} "
            f"(confidence {cls.confidence:.2f}) from {event.source_domain}. "
            f"{len(triggers)} ticker(s) survived all gates. "
            f"Half-life: {get_max_half_life(cls.event_type)}min. "
            f"Classifier reasoning: {cls.reasoning[:150]}"
        )

    def _log_audit(
        self, raw_id: str, filter_name: str, passed: bool,
        reason: Optional[str] = None, metadata: Optional[dict] = None,
    ) -> None:
        if not self.config.log_audit_to_db:
            return
        try:
            self.db.log_filter_check(raw_id, filter_name, passed, reason, metadata)
        except Exception as e:
            logger.warning("Audit log write failed for %s/%s: %s", raw_id, filter_name, e)
