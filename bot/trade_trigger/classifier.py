"""
QA Trade Trigger — Classifier
==============================

Two-level event classifier for trade actionability:

  Level 1 (Heuristic):  keyword + source weight + sentiment.
                        Fast, deterministic, free. Pre-filter.
  Level 2 (Claude API): structured classification via Sonnet 4.6.
                        Cost ~$0.003/call. Only fired when L1 score >= L1_THRESHOLD.

Output: ClassificationResult (event_type optional, actionability_score 0..10).

Anti-Bias Gate, corroboration, velocity are HANDLED ELSEWHERE (separate modules).
This file ONLY classifies "is this news potentially actionable?".

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

from .models import (
    NewsEvent, ClassificationResult, Direction, Tier
)
from .trade_trigger_mapping import is_event_supported, list_supported_events

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic configuration
# ---------------------------------------------------------------------------

# Keyword → (event_type_hint, weight). Weights scale heuristic score 0..10.
KEYWORD_RULES: List[Tuple[re.Pattern, str, float]] = [
    # Hormuz / Iran  — bidirectional: keyword can appear before OR after "Hormuz"
    (re.compile(r"\b(open|reopen|transit|deal|guide|guiding|ease|easing|safe passage|reopens?)\b.{0,80}\bhormuz\b", re.I), "hormuz_easing", 4.5),
    (re.compile(r"\bhormuz\b.{0,80}\b(open|reopen|transit|deal|guide|guiding|ease|easing|safe passage|positive|favorable)", re.I), "hormuz_easing", 4.5),
    (re.compile(r"\b(close|closes|closing|block|blocks|strike|hit|attack|mining|missile)\b.{0,80}\bhormuz\b", re.I), "hormuz_escalation", 4.8),
    (re.compile(r"\bhormuz\b.{0,80}\b(close|closes|closing|block|blocks|strike|hit|attack|shut)", re.I), "hormuz_escalation", 4.8),
    (re.compile(r"\biran\b.*\b(deal|agreement|negotiat|positive|ease|talks)", re.I), "iran_us_deal_signal", 3.8),
    (re.compile(r"\biran\b.*\b(strike|attack|missile|breakdown|reject|collapse)", re.I), "iran_us_breakdown", 4.2),
    (re.compile(r"\b(strike|airstrike|missile)\b.*\b(iran|yemen|houthi|lebanon|gaza)", re.I), "middle_east_strike", 4.0),

    # Russia / Ukraine
    (re.compile(r"\b(ukraine|russia)\b.*\b(escalat|strike|missile|nuclear|invade)", re.I), "russia_ukraine_escalation", 4.0),
    (re.compile(r"\b(ukraine|russia)\b.*\b(ceasefire|truce|peace|end of war)", re.I), "russia_ukraine_ceasefire", 4.0),

    # China / Taiwan
    (re.compile(r"\b(china|taiwan|pla)\b.*\b(invasion|drill|escalat|incursion|blockade)", re.I), "china_taiwan_tension", 3.5),
    (re.compile(r"\b(china|taiwan)\b.*\b(deescalat|talks|agreement)", re.I), "china_taiwan_deescalation", 2.8),
    (re.compile(r"\b(tariff|trade war)\b.*\b(china|chinese)", re.I), "us_china_trade_escalation", 3.2),

    # Fed / monetary
    (re.compile(r"\b(fed|powell|warsh|fomc)\b.*\b(dovish|cut|ease|accommodat|softer)", re.I), "fed_dovish_signal", 4.5),
    (re.compile(r"\b(fed|powell|warsh|fomc)\b.*\b(hawkish|hike|tighten|restrictive)", re.I), "fed_hawkish_signal", 4.0),
    (re.compile(r"\bfed\b.*\b(rate cut|cuts rates|cut by)", re.I), "fed_rate_cut_surprise", 5.0),
    (re.compile(r"\bwarsh\b.*\b(speech|statement|interview|comment|nominat)", re.I), "fed_chair_dovish_speech", 3.5),
    (re.compile(r"\b(treasury|10[\- ]?year|yield)\b.*\b(spike|surge|jump)", re.I), "treasury_yield_spike", 3.5),

    # Inflation / employment
    (re.compile(r"\bcpi\b.*\b(hot|hotter|surprise|above|exceed)", re.I), "cpi_hot", 4.0),
    (re.compile(r"\bcpi\b.*\b(cool|softer|below|miss)", re.I), "cpi_cool", 4.5),
    (re.compile(r"\b(nfp|payrolls|jobs report)\b.*\b(strong|hot|exceed|beat)", re.I), "nfp_hot", 3.5),
    (re.compile(r"\b(nfp|payrolls|jobs report)\b.*\b(weak|miss|disappoint|below)", re.I), "nfp_weak", 3.8),

    # Crypto
    (re.compile(r"\b(spot bitcoin|spot eth|spot ether)\s*etf\b.*\b(record inflow|massive inflow|biggest)", re.I), "spot_etf_inflow_record", 4.0),
    (re.compile(r"\b(spot bitcoin|spot eth|spot ether)\s*etf\b.*\b(outflow|redemption|exit)", re.I), "spot_etf_outflow_record", 4.0),
    (re.compile(r"\bofac\b.*\b(crypto|tornado|sanction|wallet)", re.I), "ofac_crypto_sanctions", 4.5),
    (re.compile(r"\bsec\b.*\b(sue|lawsuit|charge|enforcement)\b.*\b(crypto|exchange|coinbase|binance)", re.I), "sec_crypto_lawsuit_major", 4.2),
    (re.compile(r"\b(sec|cftc)\b.*\b(approve|favorable|win|drop)\b.*\b(crypto|etf)", re.I), "sec_crypto_favorable_ruling", 4.5),
    (re.compile(r"\b(hack|exploit|stolen|breach)\b.*\b(\$\d+m|\$\d+b|million|billion)", re.I), "exchange_hack_major", 4.5),
    (re.compile(r"\b(usdt|usdc|dai|fdusd)\b.*\b(depeg|de-peg|loses peg|breaks peg)", re.I), "stablecoin_depeg", 5.0),
    (re.compile(r"\b(eth|sol|ether|solana)\b.*\bspot etf\b.*\b(approve|launch)", re.I), "btc_etf_approval_altcoin", 4.8),

    # Commodities / shipping
    (re.compile(r"\b(oil|crude|opec)\b.*\b(disrupt|cut|halt|fire|attack)", re.I), "oil_supply_disruption", 4.0),
    (re.compile(r"\b(suez|panama|baltic|red sea|strait)\b.*\b(disrupt|block|attack|close)", re.I), "shipping_disruption", 3.8),

    # Systemic
    (re.compile(r"\b(bank|svb|credit suisse|signature)\b.*\b(fail|collapse|bailout|bankrupt)", re.I), "bank_failure_major", 5.0),
    (re.compile(r"\bvix\b.*\b(spike|surge|above 30|above 35|above 40)", re.I), "vix_spike_extreme", 3.5),

    # Polymarket synthetic events — distinctive headline pattern from sources/polymarket.py
    # Format: "Polymarket <Label>: odds X% → Y% (+Zpp in Nmin)"
    # We match the embedded label keywords with same weights as direct news.
    (re.compile(r"\bpolymarket\b.*\b(hormuz|iran).*\bclosure\b.*\bodds.*\b\+\d", re.I), "hormuz_escalation", 4.5),
    (re.compile(r"\bpolymarket\b.*\b(hormuz|iran).*\bclosure\b.*\bodds.*\b-\d", re.I), "hormuz_easing", 4.0),
    (re.compile(r"\bpolymarket\b.*\bus[- ]?iran[- ]?deal\b.*\bodds.*\b\+\d", re.I), "iran_us_deal_signal", 4.0),
    (re.compile(r"\bpolymarket\b.*\bfed[- ]?(june|emergency|next)?[- ]?cut\b.*\bodds.*\b\+\d", re.I), "fed_dovish_signal", 4.5),
    (re.compile(r"\bpolymarket\b.*\bfed[- ]?emergency[- ]?cut\b.*\bodds.*\b\+\d", re.I), "fed_rate_cut_surprise", 5.0),
    (re.compile(r"\bpolymarket\b.*\bukraine[- ]?ceasefire\b.*\bodds.*\b\+\d", re.I), "russia_ukraine_ceasefire", 4.0),
    (re.compile(r"\bpolymarket\b.*\bchina[- ]?taiwan[- ]?invasion\b.*\bodds.*\b\+\d", re.I), "china_taiwan_tension", 4.0),
    (re.compile(r"\bpolymarket\b.*\bus[- ]?recession\b.*\bodds.*\b\+\d", re.I), "vix_spike_extreme", 3.5),
]

# Source credibility weights (multiplier on heuristic score)
SOURCE_WEIGHTS: Dict[Tier, float] = {
    Tier.T1: 1.0,
    Tier.T2: 0.7,
    Tier.T3: 0.4,
    Tier.BANNED: 0.0,
}

# High-priority direct sources (T1+ trump factor)
DIRECT_SOURCE_BONUS: Dict[str, float] = {
    "truthsocial.com": 1.5,
    "trumpstruth.org": 1.5,
    "whitehouse.gov": 1.5,
    "state.gov": 1.4,
    "federalreserve.gov": 1.5,
    "treasury.gov": 1.4,
    "ofac.treasury.gov": 1.6,
    "reuters.com": 1.2,
    "bloomberg.com": 1.2,
    "wsj.com": 1.1,
    "cnbc.com": 1.0,
    "lbma.org.uk": 1.2,
    "cmegroup.com": 1.2,
    "polymarket.com": 1.4,            # leading indicator — odds shift before mainstream news
}

L1_THRESHOLD = 4.0      # below this → no Claude call, return non-actionable
L2_THRESHOLD = 7.0      # above this in final score → actionable


# ---------------------------------------------------------------------------
# Heuristic scorer
# ---------------------------------------------------------------------------

class HeuristicScorer:
    """Level-1 fast scorer. No external calls."""

    def score(self, event: NewsEvent) -> ClassificationResult:
        text = f"{event.headline}\n{event.body}"
        matches: List[Tuple[str, float, str]] = []  # (event_type, weight, matched_keyword)

        for pattern, event_type, weight in KEYWORD_RULES:
            m = pattern.search(text)
            if m:
                matches.append((event_type, weight, m.group(0)))

        if not matches:
            return ClassificationResult(
                event_type=None,
                actionable=False,
                actionability_score=0.0,
                direction_hint=None,
                asset_class_hint=None,
                half_life_minutes=0,
                confidence=0.9,
                reasoning="No matching keywords",
                raw_keywords_matched=[],
            )

        # Pick highest-weight match as primary event_type
        matches.sort(key=lambda x: -x[1])
        primary_event, base_weight, _ = matches[0]

        # Apply source multipliers
        source_mult = SOURCE_WEIGHTS.get(event.source_tier, 0.0)
        domain_bonus = DIRECT_SOURCE_BONUS.get(event.source_domain.lower(), 1.0)
        final_score = base_weight * source_mult * domain_bonus
        final_score = min(final_score, 10.0)

        return ClassificationResult(
            event_type=primary_event if is_event_supported(primary_event) else None,
            actionable=final_score >= L1_THRESHOLD,
            actionability_score=round(final_score, 2),
            direction_hint=None,
            asset_class_hint=None,
            half_life_minutes=0,
            confidence=0.6,  # heuristic, not high
            reasoning=f"Heuristic: matched {len(matches)} rules. Primary: {primary_event} (w={base_weight}). Source: {event.source_domain} (mult={source_mult * domain_bonus:.2f})",
            raw_keywords_matched=[m[2] for m in matches[:3]],
        )


# ---------------------------------------------------------------------------
# Claude classifier (Level 2)
# ---------------------------------------------------------------------------

CLAUDE_SYSTEM_PROMPT = """You are a quantitative event classifier for an institutional trading system.
Your job: classify if a news event is trade-actionable in the next 0-2 hours.

You output STRICT JSON only. No prose. No markdown fences. No commentary.

Schema:
{
  "event_type": "string (one of: " + supported_events + " or null if no match)",
  "actionable": boolean,
  "score": float 0..10,
  "direction_hint": "long" | "short" | "skip" | null,
  "asset_class_hint": "crypto" | "equity" | "commodity" | "fx" | "rates" | null,
  "half_life_minutes": integer (window for 80% price reaction),
  "confidence": float 0..1,
  "reasoning": "1-2 sentence justification, factual only"
}

Rules:
- If event is already 2+ hours old by language ("yesterday", "last week"), score <= 4.
- If event is rumor/unconfirmed, score <= 5 even if dramatic.
- If event was already widely reported (you can tell from framing), score <= 4 (priced in).
- BTC, COPX, URA, KTOS are NEVER traded; do not recommend them.
- Be conservative. False positives waste capital.
"""


@dataclass
class ClaudeClassifierConfig:
    api_key: Optional[str] = None
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 400
    temperature: float = 0.0
    timeout_seconds: float = 8.0


class ClaudeClassifier:
    """Level-2 classifier. Calls Anthropic API. Falls back gracefully on error."""

    def __init__(self, config: Optional[ClaudeClassifierConfig] = None):
        self.config = config or ClaudeClassifierConfig()
        self.api_key = self.config.api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError:
                raise RuntimeError(
                    "anthropic package not installed. Run: pip install anthropic"
                )
            if not self.api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = Anthropic(api_key=self.api_key)
        return self._client

    def classify(self, event: NewsEvent) -> ClassificationResult:
        """Call Claude API. On error, return low-confidence non-actionable result."""
        try:
            client = self._get_client()
            supported = ", ".join(list_supported_events())
            system = CLAUDE_SYSTEM_PROMPT.replace('" + supported_events + "', supported)

            user_msg = (
                f"Headline: {event.headline}\n\n"
                f"Body: {event.body[:2000]}\n\n"
                f"Source: {event.source_domain} (tier={event.source_tier.value})\n"
                f"Published: {event.published_at.isoformat()}\n"
                f"Age (minutes): {event.age_minutes():.1f}\n\n"
                f"Output JSON only."
            )

            resp = client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = resp.content[0].text.strip()
            # Strip accidental fences
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
            data = json.loads(text)

            return ClassificationResult(
                event_type=data.get("event_type") if data.get("event_type") and is_event_supported(data["event_type"]) else None,
                actionable=bool(data.get("actionable", False)),
                actionability_score=float(data.get("score", 0.0)),
                direction_hint=Direction(data["direction_hint"]) if data.get("direction_hint") in {"long", "short", "skip"} else None,
                asset_class_hint=data.get("asset_class_hint"),
                half_life_minutes=int(data.get("half_life_minutes", 0)),
                confidence=float(data.get("confidence", 0.7)),
                reasoning=data.get("reasoning", "")[:500],
                raw_keywords_matched=[],
            )
        except Exception as e:
            logger.warning("Claude classification failed: %s", e)
            return ClassificationResult(
                event_type=None,
                actionable=False,
                actionability_score=0.0,
                direction_hint=None,
                asset_class_hint=None,
                half_life_minutes=0,
                confidence=0.0,
                reasoning=f"L2 failed: {type(e).__name__}",
                raw_keywords_matched=[],
            )


# ---------------------------------------------------------------------------
# Pipeline classifier (combines L1 + L2)
# ---------------------------------------------------------------------------

class TradeTriggerClassifier:
    """
    Public entry point.
    Pipeline:
      1. Heuristic L1 score.
      2. If L1 >= L1_THRESHOLD AND tier in {T1, T2}, call Claude L2.
      3. Merge: take Claude's event_type/score if L2 confidence > 0.5,
         else fall back to L1 result.
    """

    def __init__(
        self,
        claude_config: Optional[ClaudeClassifierConfig] = None,
        enable_l2: bool = True,
    ):
        self.heuristic = HeuristicScorer()
        self.claude = ClaudeClassifier(claude_config) if enable_l2 else None
        self.enable_l2 = enable_l2

    def classify(self, event: NewsEvent) -> ClassificationResult:
        # L1
        l1 = self.heuristic.score(event)
        logger.info(
            "L1: event=%s score=%.2f source=%s",
            l1.event_type, l1.actionability_score, event.source_domain,
        )

        # Banned source — never proceed
        if event.source_tier == Tier.BANNED:
            return ClassificationResult(
                event_type=None,
                actionable=False,
                actionability_score=0.0,
                direction_hint=None,
                asset_class_hint=None,
                half_life_minutes=0,
                confidence=1.0,
                reasoning="Source is banned (Tier=BANNED)",
                raw_keywords_matched=l1.raw_keywords_matched,
            )

        # If L1 below threshold or T2 disabled — return L1
        if not self.enable_l2 or l1.actionability_score < L1_THRESHOLD:
            return l1

        # Don't waste API calls on T3 sources
        if event.source_tier == Tier.T3:
            l1.reasoning += " [L2 skipped: T3 source]"
            return l1

        # L2
        l2 = self.claude.classify(event)
        logger.info(
            "L2: event=%s score=%.2f conf=%.2f",
            l2.event_type, l2.actionability_score, l2.confidence,
        )

        # Merge: prefer L2 if confidence high, else stick with L1
        if l2.confidence >= 0.5 and l2.actionability_score > 0:
            # Combine: average L1 and L2 scores weighted by L2 confidence
            merged_score = l2.actionability_score * l2.confidence + l1.actionability_score * (1 - l2.confidence)
            return ClassificationResult(
                event_type=l2.event_type or l1.event_type,
                actionable=merged_score >= L2_THRESHOLD,
                actionability_score=round(merged_score, 2),
                direction_hint=l2.direction_hint,
                asset_class_hint=l2.asset_class_hint,
                half_life_minutes=l2.half_life_minutes,
                confidence=l2.confidence,
                reasoning=f"L1+L2 merged. L1: {l1.reasoning[:120]} | L2: {l2.reasoning[:200]}",
                raw_keywords_matched=l1.raw_keywords_matched,
            )

        # L2 failed or low confidence — return L1 with note
        l1.reasoning += f" [L2 unreliable: conf={l2.confidence:.2f}]"
        return l1
