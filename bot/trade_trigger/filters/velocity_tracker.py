"""
QA Trade Trigger — Velocity Filter
====================================

Rejects stale events. Rationale: by the time an event is 2+ hours old,
institutional traders (Bloomberg Terminal, Reuters wire, broker squawk boxes)
have already moved on it. Trading on stale news = chasing FOMO at the peak.

Usage:
    tracker = VelocityTracker(max_age_minutes=120)
    if tracker.is_fresh(event):
        # proceed with classification
        ...

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from ..models import NewsEvent

logger = logging.getLogger(__name__)


@dataclass
class VelocityConfig:
    """Velocity filter thresholds (minutes)."""
    max_age_fresh: float = 30.0       # below this — clean fresh event
    max_age_acceptable: float = 120.0  # above this — REJECT (too stale)
    # Between fresh and acceptable: pass but flag as "decaying"


class VelocityTracker:
    """Filter events by recency. Stateless, deterministic.

    Decision tree:
        age <= max_age_fresh        → FRESH    (full conviction)
        age <= max_age_acceptable   → DECAYING (pass, but classifier should
                                                downgrade conviction)
        age >  max_age_acceptable   → STALE    (REJECT, do not classify)
    """

    def __init__(self, config: Optional[VelocityConfig] = None):
        self.config = config or VelocityConfig()

    def is_fresh(self, event: NewsEvent, now: Optional[datetime] = None) -> bool:
        """True if event passes velocity filter (within max_age_acceptable)."""
        return self.check(event, now)[0]

    def check(
        self, event: NewsEvent, now: Optional[datetime] = None,
    ) -> Tuple[bool, str, float]:
        """Full check returning (passed, status, age_minutes).

        status: 'fresh' / 'decaying' / 'stale'
        """
        age = event.age_minutes(now)

        if age < 0:
            # Event published in the future — clock skew or bad data
            logger.warning(
                "Event %s has negative age (%.1f min) — clock skew or bad timestamp",
                event.raw_id, age,
            )
            return (True, "fresh", age)  # treat as fresh, log for inspection

        if age <= self.config.max_age_fresh:
            return (True, "fresh", age)

        if age <= self.config.max_age_acceptable:
            return (True, "decaying", age)

        return (False, "stale", age)

    def conviction_decay_factor(
        self, event: NewsEvent, now: Optional[datetime] = None,
    ) -> float:
        """Linear decay multiplier 0..1 based on age.

        Use this to scale TradeSignal conviction:
            signal.conviction *= tracker.conviction_decay_factor(event)

        Returns 1.0 if fresh, 0.0 if stale, linear in between.
        """
        age = max(0.0, event.age_minutes(now))
        if age <= self.config.max_age_fresh:
            return 1.0
        if age >= self.config.max_age_acceptable:
            return 0.0
        # Linear decay between fresh and acceptable
        span = self.config.max_age_acceptable - self.config.max_age_fresh
        consumed = age - self.config.max_age_fresh
        return max(0.0, 1.0 - consumed / span)
