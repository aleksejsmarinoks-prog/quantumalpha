"""
QA Trade Trigger — Shared Data Models
======================================

Dataclasses, shared by classifier, asset_mapping, corroboration_gate.
Designed for production use: type-safe, JSON-serializable, immutable where possible.

Author: QuantumAlpha
Version: 0.1.0
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List
import json


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    SKIP = "skip"


class Tier(str, Enum):
    """Source credibility tier. Tier-1 only sources allowed for pricing/triggers."""
    T1 = "tier1"   # Reuters, Bloomberg, WSJ, WhiteHouse, State, Fed, Treasury, official accounts
    T2 = "tier2"   # CoinDesk, Axios, FT, CNBC
    T3 = "tier3"   # Aggregators, secondary
    BANNED = "banned"  # News24, Indian aggregators, etc. — never used


class TriggerVerdict(str, Enum):
    FIRE = "fire"            # All gates passed → push to Trade Trigger bot
    SKIP_PRICED_IN = "skip_priced_in"   # Anti-Bias Gate: already moved >5% / RSI>70
    SKIP_STALE = "skip_stale"           # Velocity gate: event >2h old
    SKIP_LOW_CONVICTION = "skip_low_conviction"
    SKIP_NO_CORROBORATION = "skip_no_corroboration"
    CONTEXT_ONLY = "context_only"       # Send to Diplomatic Feed bot, not trade bot


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsEvent:
    """Raw news event from any source. Immutable."""
    headline: str
    body: str
    source_url: str
    source_domain: str
    source_tier: Tier
    published_at: datetime          # UTC
    fetched_at: datetime            # UTC
    raw_id: str                     # hash for dedup

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.published_at).total_seconds() / 60.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published_at"] = self.published_at.isoformat()
        d["fetched_at"] = self.fetched_at.isoformat()
        d["source_tier"] = self.source_tier.value
        return d


@dataclass
class AssetTrigger:
    """One ticker-direction pair within a TradeSignal."""
    ticker: str
    venue: str                      # "Bybit", "T212", "CME"
    direction: Direction
    conviction: float               # 0..1
    suggested_size_pct_bucket: float  # % of bucket cap
    invalidation_price: Optional[float] = None
    invalidation_reason: Optional[str] = None
    half_life_minutes: int = 90     # decay window


@dataclass
class TradeSignal:
    """Final output: what gets pushed to Trade Trigger bot.
    Generated only when ALL 5 gates pass (corroboration, velocity, anti-bias,
    actionability, mapping).
    """
    event_type: str                 # mapped event key from asset_mapping
    triggers: List[AssetTrigger]    # 1+ ticker triggers
    sources: List[str]              # corroborating source URLs
    first_seen_utc: datetime
    actionability_score: float      # 0..10, threshold = 7
    reasoning: str
    verdict: TriggerVerdict = TriggerVerdict.FIRE

    def to_telegram_payload(self) -> str:
        """Format for Bot #2 push. Cyrillic-safe."""
        lines = [
            "🚨 TRADE TRIGGER 🚨",
            f"Event: {self.event_type}",
            f"Score: {self.actionability_score:.1f}/10",
            f"Sources: {len(self.sources)} ✅",
            ""
        ]
        for t in self.triggers:
            lines.append(f"▸ {t.ticker} ({t.venue}): {t.direction.value.upper()}")
            lines.append(f"  conviction={t.conviction:.2f}, size={t.suggested_size_pct_bucket:.1f}% bucket")
            if t.invalidation_price:
                lines.append(f"  invalidation: {t.invalidation_price} ({t.invalidation_reason})")
            lines.append(f"  half-life: {t.half_life_minutes}min")
            lines.append("")
        lines.append(f"Reasoning: {self.reasoning}")
        return "\n".join(lines)

    def to_json(self) -> str:
        d = {
            "event_type": self.event_type,
            "verdict": self.verdict.value,
            "score": self.actionability_score,
            "first_seen_utc": self.first_seen_utc.isoformat(),
            "sources": self.sources,
            "reasoning": self.reasoning,
            "triggers": [
                {
                    "ticker": t.ticker, "venue": t.venue, "direction": t.direction.value,
                    "conviction": t.conviction, "size_pct": t.suggested_size_pct_bucket,
                    "invalidation_price": t.invalidation_price,
                    "invalidation_reason": t.invalidation_reason,
                    "half_life_min": t.half_life_minutes
                } for t in self.triggers
            ]
        }
        return json.dumps(d, ensure_ascii=False, indent=2)


@dataclass
class ClassificationResult:
    """Output of classifier (before mapping/gates applied)."""
    event_type: Optional[str]       # None if not mapped
    actionable: bool
    actionability_score: float      # 0..10
    direction_hint: Optional[Direction]
    asset_class_hint: Optional[str]  # "crypto" / "equity" / "commodity" / "fx"
    half_life_minutes: int
    confidence: float               # 0..1, classifier self-confidence
    reasoning: str
    raw_keywords_matched: List[str] = field(default_factory=list)
