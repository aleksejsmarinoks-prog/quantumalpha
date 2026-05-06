"""
QA Trade Trigger — Polymarket Odds Watcher
============================================

Leading indicator source. Free public API, no auth.

Why Polymarket matters
----------------------
On big binary geopolitical events, Polymarket odds shift FASTER than RSS news
hits Reuters or Bloomberg. Smart money front-runs odds 1-5 minutes before
mainstream coverage. We treat a significant odds shift as a synthetic
NewsEvent — fed into the same classifier pipeline as real news.

Example: "Iran closes Hormuz in May 2026" market.
  - Baseline odds: 18%
  - Sudden shift to 45% in 8 minutes
  → emit NewsEvent("Polymarket: Iran-Hormuz-closure odds spike 18%→45%")
  → classifier maps to hormuz_escalation event_type
  → enters main pipeline like any other source

API
---
Gamma API (https://gamma-api.polymarket.com/):
  GET /markets?slug=<slug>          → single market by slug
  GET /markets?active=true&closed=false&limit=100  → list active markets

Rate limits: ~10 req/s without auth. We poll our 5-10 watched markets every
2-5 minutes → trivially under limit.

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from typing import Dict, List, Optional, TYPE_CHECKING

from ..models import NewsEvent, Tier

if TYPE_CHECKING:
    from ..db import TradeTriggerDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Watchlist — markets we care about
# ---------------------------------------------------------------------------

@dataclass
class MarketSpec:
    """Configuration for one watched market.

    Fields:
      slug:           Polymarket market slug (URL-style identifier)
      outcome:        outcome name to track (e.g. "Yes" / "No" / "Iran")
      event_type:     mapped QA event_type when significant shift detected
      shift_threshold: minimum absolute price change (in 0..1) to trigger
                      (default 0.05 = 5 percentage points)
      direction:      'up' | 'down' | 'either' — which direction emits event
    """
    slug: str
    outcome: str
    event_type: str
    shift_threshold: float = 0.05
    direction: str = "either"   # 'up' / 'down' / 'either'
    label: str = ""             # human-readable for headlines


# Default watchlist — covers QA's primary regime drivers.
# Update this list as new high-stakes markets appear on Polymarket.
DEFAULT_WATCHLIST: List[MarketSpec] = [
    # Iran / Hormuz
    MarketSpec(
        slug="will-iran-close-the-strait-of-hormuz-in-may-2026",
        outcome="Yes",
        event_type="hormuz_escalation",
        direction="up",
        shift_threshold=0.05,
        label="Iran-Hormuz-closure",
    ),
    MarketSpec(
        slug="will-iran-close-the-strait-of-hormuz-in-may-2026",
        outcome="Yes",
        event_type="hormuz_easing",
        direction="down",
        shift_threshold=0.05,
        label="Iran-Hormuz-closure",
    ),
    MarketSpec(
        slug="us-iran-deal-by-may-2026",
        outcome="Yes",
        event_type="iran_us_deal_signal",
        direction="up",
        shift_threshold=0.07,
        label="US-Iran-deal",
    ),

    # Fed / monetary
    MarketSpec(
        slug="fed-rate-cut-in-june-2026",
        outcome="Yes",
        event_type="fed_dovish_signal",
        direction="up",
        shift_threshold=0.07,
        label="Fed-June-cut",
    ),
    MarketSpec(
        slug="fed-emergency-rate-cut-2026",
        outcome="Yes",
        event_type="fed_rate_cut_surprise",
        direction="up",
        shift_threshold=0.10,
        label="Fed-emergency-cut",
    ),

    # Russia / Ukraine
    MarketSpec(
        slug="ukraine-russia-ceasefire-by-end-of-2026",
        outcome="Yes",
        event_type="russia_ukraine_ceasefire",
        direction="up",
        shift_threshold=0.07,
        label="Ukraine-ceasefire",
    ),

    # China / Taiwan
    MarketSpec(
        slug="china-invades-taiwan-by-end-of-2026",
        outcome="Yes",
        event_type="china_taiwan_tension",
        direction="up",
        shift_threshold=0.05,
        label="China-Taiwan-invasion",
    ),

    # Crypto-specific
    MarketSpec(
        slug="us-recession-in-2026",
        outcome="Yes",
        event_type="vix_spike_extreme",
        direction="up",
        shift_threshold=0.10,
        label="US-recession",
    ),
]


# ---------------------------------------------------------------------------
# Polymarket HTTP client (lazy httpx import for testability)
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"


class PolymarketClient:
    """Thin async wrapper around Polymarket Gamma API.

    Stateless. One client instance can serve many markets.
    Constructor accepts an optional session for testing/dependency-injection.
    """

    def __init__(
        self,
        base_url: str = GAMMA_BASE,
        timeout_seconds: float = 8.0,
        http_client=None,                # optional pre-built httpx.AsyncClient
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._http_client = http_client

    async def _get_client(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=self.timeout)
        return self._http_client

    async def fetch_market_by_slug(self, slug: str) -> Optional[dict]:
        """Returns raw market dict or None on miss/failure."""
        client = await self._get_client()
        url = f"{self.base_url}/markets"
        try:
            resp = await client.get(url, params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
            # Gamma can return list or single object — normalize
            if isinstance(data, list):
                return data[0] if data else None
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("Polymarket fetch %s failed: %s", slug, e)
            return None

    @staticmethod
    def extract_outcome_price(market: dict, outcome_name: str) -> Optional[float]:
        """Pull price for a named outcome from a Gamma market response.

        Gamma markets have a few possible shapes:
          - market["outcomes"] = ["Yes", "No"] + market["outcomePrices"] = ["0.18", "0.82"]
          - market["tokens"] = [{"outcome": "Yes", "price": "0.18"}, ...]
        We try both.
        """
        # Shape 1: parallel arrays
        outcomes = market.get("outcomes")
        prices = market.get("outcomePrices") or market.get("outcome_prices")
        if isinstance(outcomes, list) and isinstance(prices, list):
            for name, price_str in zip(outcomes, prices):
                if str(name).strip().lower() == outcome_name.strip().lower():
                    try:
                        return float(price_str)
                    except (ValueError, TypeError):
                        return None

        # Shape 2: tokens list
        tokens = market.get("tokens") or market.get("outcomes_tokens")
        if isinstance(tokens, list):
            for tok in tokens:
                if not isinstance(tok, dict):
                    continue
                name = tok.get("outcome") or tok.get("name")
                price = tok.get("price")
                if name and str(name).strip().lower() == outcome_name.strip().lower():
                    try:
                        return float(price) if price is not None else None
                    except (ValueError, TypeError):
                        return None

        return None

    async def close(self) -> None:
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Shift detector
# ---------------------------------------------------------------------------

@dataclass
class OddsShift:
    market_slug: str
    outcome_name: str
    label: str
    event_type: str
    previous_price: float
    current_price: float
    delta: float                # current - previous (signed)
    window_minutes: int
    direction: str              # 'up' / 'down'

    def headline(self) -> str:
        pct_prev = self.previous_price * 100
        pct_curr = self.current_price * 100
        arrow = "→"
        return (
            f"Polymarket {self.label}: odds {pct_prev:.0f}% {arrow} {pct_curr:.0f}% "
            f"({'+' if self.delta >= 0 else ''}{self.delta * 100:.0f}pp in {self.window_minutes}min)"
        )

    def body(self) -> str:
        return (
            f"Polymarket market '{self.market_slug}' outcome '{self.outcome_name}' "
            f"shifted from {self.previous_price:.3f} to {self.current_price:.3f} "
            f"({self.delta * 100:+.1f} percentage points) within {self.window_minutes} "
            f"minutes. Direction: {self.direction}. Mapped event_type: {self.event_type}."
        )


# ---------------------------------------------------------------------------
# Polymarket Watcher — main orchestration class
# ---------------------------------------------------------------------------

@dataclass
class WatcherConfig:
    poll_interval_seconds: int = 180        # 3 min default
    shift_window_minutes: int = 15          # compare current vs window's earliest price
    history_keep_days: int = 7
    source_name: str = "polymarket"


class PolymarketWatcher:
    """Polls watched markets, detects significant shifts, emits NewsEvents."""

    def __init__(
        self,
        db: "TradeTriggerDB",
        client: Optional[PolymarketClient] = None,
        watchlist: Optional[List[MarketSpec]] = None,
        config: Optional[WatcherConfig] = None,
    ):
        self.db = db
        self.client = client or PolymarketClient()
        self.watchlist = watchlist or DEFAULT_WATCHLIST
        self.config = config or WatcherConfig()

    # -----------------------------------------------------------------------
    # Single poll cycle
    # -----------------------------------------------------------------------

    async def poll_once(self, now: Optional[datetime] = None) -> List[NewsEvent]:
        """Poll all watched markets once. Returns list of newly emitted NewsEvents."""
        now = now or datetime.now(timezone.utc)
        emitted: List[NewsEvent] = []
        success_count = 0
        error_msg: Optional[str] = None

        # Markets may be duplicated across watchlist (same slug, different
        # event_types per direction) — fetch each unique slug only once.
        slug_cache: Dict[str, Optional[dict]] = {}

        for spec in self.watchlist:
            try:
                if spec.slug not in slug_cache:
                    slug_cache[spec.slug] = await self.client.fetch_market_by_slug(spec.slug)
                market = slug_cache[spec.slug]

                if market is None:
                    logger.debug("Market %s not available", spec.slug)
                    continue

                price = self.client.extract_outcome_price(market, spec.outcome)
                if price is None:
                    logger.debug(
                        "Outcome '%s' not found in market %s",
                        spec.outcome, spec.slug,
                    )
                    continue

                # Persist current observation
                self.db.insert_polymarket_odds(spec.slug, spec.outcome, price)

                # Detect shift vs window-earliest price
                shift = self._detect_shift(spec, price, now=now)
                if shift is not None:
                    event = self._shift_to_event(shift, now=now)
                    emitted.append(event)
                    logger.info(
                        "Polymarket shift detected: %s %.2f→%.2f (%+.2fpp)",
                        spec.label, shift.previous_price, shift.current_price,
                        shift.delta * 100,
                    )

                success_count += 1
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                logger.warning("Watcher poll error on %s: %s", spec.slug, e)

        # Update source health
        try:
            self.db.update_source_health(
                self.config.source_name,
                success=(success_count > 0 or len(self.watchlist) == 0),
                events_added=len(emitted),
                error=error_msg if success_count == 0 else None,
            )
        except Exception as e:
            logger.warning("Failed to update source_health: %s", e)

        # Prune old data periodically (cheap, run every poll)
        try:
            self.db.prune_polymarket_history(keep_days=self.config.history_keep_days)
        except Exception:
            pass

        # Heartbeat
        try:
            self.db.pulse(
                self.config.source_name,
                {"polled_markets": len(slug_cache), "events_emitted": len(emitted)},
            )
        except Exception:
            pass

        return emitted

    # -----------------------------------------------------------------------
    # Shift detection logic
    # -----------------------------------------------------------------------

    def _detect_shift(
        self, spec: MarketSpec, current_price: float,
        now: Optional[datetime] = None,
    ) -> Optional[OddsShift]:
        """Compare current vs earliest-in-window price. Return OddsShift if
        threshold + direction match, else None.
        """
        history = self.db.polymarket_odds_window(
            spec.slug, spec.outcome,
            within_minutes=self.config.shift_window_minutes,
            now=now,
        )
        # Need at least 2 observations (current + 1 earlier)
        if len(history) < 2:
            return None

        # Newest is at history[0] (just inserted). Compare against earliest in window.
        # Use earliest-in-window (most lookback) for shift magnitude.
        earliest_price, _ = history[-1]
        delta = current_price - earliest_price

        if abs(delta) < spec.shift_threshold:
            return None

        observed_dir = "up" if delta > 0 else "down"
        if spec.direction != "either" and spec.direction != observed_dir:
            return None

        return OddsShift(
            market_slug=spec.slug,
            outcome_name=spec.outcome,
            label=spec.label or spec.slug,
            event_type=spec.event_type,
            previous_price=earliest_price,
            current_price=current_price,
            delta=delta,
            window_minutes=self.config.shift_window_minutes,
            direction=observed_dir,
        )

    # -----------------------------------------------------------------------
    # Synthetic event creation
    # -----------------------------------------------------------------------

    def _shift_to_event(
        self, shift: OddsShift, now: Optional[datetime] = None,
    ) -> NewsEvent:
        """Convert OddsShift → NewsEvent for classifier ingestion.

        Tier: T1 (Polymarket is institutional-grade signal source).
        Domain: 'polymarket.com' — recognizable for downstream filters.
        raw_id: deterministic hash of (slug, outcome, current_price, window)
                to dedup if same shift observed twice in close succession.
        """
        now = now or datetime.now(timezone.utc)
        # Truncate hash to manageable length, deterministic per shift episode
        hash_input = (
            f"{shift.market_slug}|{shift.outcome_name}|"
            f"{shift.current_price:.4f}|{now.strftime('%Y%m%d%H%M')}"
        )
        raw_id = "pm_" + sha1(hash_input.encode()).hexdigest()[:16]

        url = f"https://polymarket.com/event/{shift.market_slug}"
        return NewsEvent(
            headline=shift.headline(),
            body=shift.body(),
            source_url=url,
            source_domain="polymarket.com",
            source_tier=Tier.T1,
            published_at=now,
            fetched_at=now,
            raw_id=raw_id,
        )

    # -----------------------------------------------------------------------
    # Background loop
    # -----------------------------------------------------------------------

    async def run_forever(self, on_event=None) -> None:
        """Long-running background loop. on_event(NewsEvent) called for each emit."""
        logger.info(
            "PolymarketWatcher starting: %d markets, %ds interval",
            len(self.watchlist), self.config.poll_interval_seconds,
        )
        while True:
            try:
                events = await self.poll_once()
                if on_event is not None:
                    for ev in events:
                        try:
                            res = on_event(ev)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as e:
                            logger.exception("on_event handler raised: %s", e)
            except asyncio.CancelledError:
                logger.info("PolymarketWatcher cancelled")
                break
            except Exception as e:
                logger.exception("Polling cycle failed: %s", e)

            await asyncio.sleep(self.config.poll_interval_seconds)
