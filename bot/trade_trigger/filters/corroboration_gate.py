"""
QA Trade Trigger — Corroboration Gate
=======================================

Closes the QA design-spec gap: "Cross-source corroboration gate for CRITICAL
diplomatic alerts" (from project backlog).

Single Reuters tweet alone should NOT fire a trade signal. Markets get spoofed
by single-source rumors regularly. We require ≥2 independent Tier-1 sources
reporting the same event within a sliding window.

Logic:
    1. Extract topic keywords from event headline (proper nouns + key verbs)
    2. Query DB for other recent events containing those keywords
    3. Count distinct source_domain among matches
    4. Pass if distinct_sources >= min_corroborators

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple, TYPE_CHECKING

from ..models import NewsEvent, Tier

if TYPE_CHECKING:
    from ..db import TradeTriggerDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Topic extraction helpers
# ---------------------------------------------------------------------------

# High-signal keywords: proper nouns and event-defining terms.
TOPIC_PROPER_NOUNS = re.compile(
    r"\b("
    r"Hormuz|Iran|Israel|Ukraine|Russia|China|Taiwan|"
    r"Fed|FOMC|Powell|Warsh|Treasury|"
    r"Trump|Biden|Putin|Xi|"
    r"OFAC|SEC|CFTC|"
    r"BTC|Bitcoin|ETH|Ethereum|Solana|SOL|"
    r"USDC|USDT|DAI|"
    r"Brent|WTI|crude|oil|gold|silver|"
    r"Coinbase|Binance|Tether|Circle|"
    r"NATO|OPEC|EU|UN"
    r")\b",
    re.IGNORECASE,
)

# Event-action verbs that define what happened
TOPIC_VERBS = re.compile(
    r"\b("
    r"strike|attack|invade|missile|"
    r"ceasefire|deal|agreement|truce|"
    r"hack|exploit|breach|"
    r"rate cut|rate hike|cut rates|hike rates|"
    r"depeg|collapse|bankrupt|fail|"
    r"sanction|tariff"
    r")\b",
    re.IGNORECASE,
)


def extract_topic_keywords(headline: str, max_keywords: int = 4) -> List[str]:
    """Extract distinctive proper nouns + verbs for cross-source matching.

    Returns list of unique keywords (lowercased) ordered by length desc
    (longer = more distinctive).
    """
    nouns = {m.group(0).lower() for m in TOPIC_PROPER_NOUNS.finditer(headline)}
    verbs = {m.group(0).lower() for m in TOPIC_VERBS.finditer(headline)}
    combined = sorted(nouns | verbs, key=lambda w: -len(w))
    return combined[:max_keywords]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class CorroborationConfig:
    min_distinct_sources: int = 2          # require at least 2 different domains
    window_minutes: int = 15               # look back this far
    require_tier1_count: int = 1           # at least N Tier-1 sources among matches
    direct_source_bypass: bool = True      # Trump/WH/Fed direct → no corroboration
    direct_source_domains: Set[str] = field(default_factory=lambda: {
        "truthsocial.com",
        "trumpstruth.org",
        "whitehouse.gov",
        "federalreserve.gov",
        "treasury.gov",
        "ofac.treasury.gov",
    })


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

@dataclass
class CorroborationResult:
    passed: bool
    distinct_sources: int
    matching_event_ids: List[str]
    reason: str
    tier1_match_count: int = 0
    bypassed_direct_source: bool = False


class CorroborationGate:
    """Cross-source verification for trade-actionable events.

    Two pass conditions (any of):
      A) Direct authoritative source (e.g. Truth Social Trump post,
         Fed statement): bypass corroboration — these ARE the source.
      B) ≥ min_distinct_sources distinct domains reporting same topic
         within window_minutes, AND ≥ require_tier1_count of them are Tier-1.
    """

    def __init__(
        self,
        db: "TradeTriggerDB",
        config: Optional[CorroborationConfig] = None,
    ):
        self.db = db
        self.config = config or CorroborationConfig()

    def check(
        self, event: NewsEvent, now: Optional[datetime] = None,
    ) -> CorroborationResult:
        # Path A: direct authoritative source bypass
        if (
            self.config.direct_source_bypass
            and event.source_domain.lower() in self.config.direct_source_domains
            and event.source_tier == Tier.T1
        ):
            return CorroborationResult(
                passed=True,
                distinct_sources=1,
                matching_event_ids=[event.raw_id],
                reason=f"Direct authoritative source bypass: {event.source_domain}",
                tier1_match_count=1,
                bypassed_direct_source=True,
            )

        # Path B: cross-source corroboration via DB lookup
        keywords = extract_topic_keywords(event.headline)
        if not keywords:
            return CorroborationResult(
                passed=False,
                distinct_sources=0,
                matching_event_ids=[],
                reason="No distinctive keywords extracted from headline",
            )

        # Query DB for recent events matching ANY of the top keywords.
        # Events with multiple keyword matches will have higher relevance.
        all_matches: List[Tuple[str, str, str]] = []
        for kw in keywords:
            all_matches.extend(
                self.db.recent_events_for_topic(
                    kw, within_minutes=self.config.window_minutes, now=now,
                )
            )

        # Dedup by raw_id — count distinct sources
        seen_event_ids: Set[str] = set()
        distinct_domains: Set[str] = set()
        tier1_count = 0
        match_ids: List[str] = []

        for raw_id, source_domain, _published in all_matches:
            if raw_id in seen_event_ids:
                continue
            seen_event_ids.add(raw_id)
            distinct_domains.add(source_domain.lower())
            match_ids.append(raw_id)
            # Check tier (need to fetch event for tier — small N, acceptable cost)
            ev = self.db.get_event(raw_id)
            if ev and ev.source_tier == Tier.T1:
                tier1_count += 1

        # Include the current event itself in counts
        distinct_domains.add(event.source_domain.lower())
        if event.source_tier == Tier.T1:
            tier1_count += 1
        if event.raw_id not in seen_event_ids:
            match_ids.append(event.raw_id)

        passed = (
            len(distinct_domains) >= self.config.min_distinct_sources
            and tier1_count >= self.config.require_tier1_count
        )

        reason = (
            f"distinct_sources={len(distinct_domains)} "
            f"(req≥{self.config.min_distinct_sources}), "
            f"tier1_count={tier1_count} (req≥{self.config.require_tier1_count}), "
            f"window={self.config.window_minutes}min, "
            f"keywords={keywords}"
        )

        return CorroborationResult(
            passed=passed,
            distinct_sources=len(distinct_domains),
            matching_event_ids=match_ids,
            reason=reason,
            tier1_match_count=tier1_count,
        )
