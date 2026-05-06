"""
QA Trade Trigger — RSS Source Base
=====================================

Common scaffolding for RSS-driven sources (WhiteHouse, OFAC, State Dept).

Each concrete source extends RSSWatcherBase and configures:
  - feed URL
  - source_domain (used by classifier source weights)
  - source_tier (T1 for direct authoritative)
  - source_name (used in DB source_health)

Author: QuantumAlpha
Version: 0.4.0
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import List, Optional, TYPE_CHECKING

from ..models import NewsEvent, Tier

if TYPE_CHECKING:
    from ..db import TradeTriggerDB

logger = logging.getLogger(__name__)


@dataclass
class RSSWatcherConfig:
    poll_interval_seconds: int = 300       # 5 min default
    max_items_per_poll: int = 50           # cap to avoid flooding on first run
    user_agent: str = "QA-Trade-Trigger/0.4 (RSS poller)"
    timeout_seconds: float = 10.0


class RSSWatcherBase:
    """Base class. Subclasses MUST set: feed_url, source_domain, source_tier,
    source_name (class attributes or in __init__).
    """

    feed_url: str = ""
    source_domain: str = ""
    source_tier: Tier = Tier.T1
    source_name: str = "rss"

    def __init__(
        self,
        db: "TradeTriggerDB",
        config: Optional[RSSWatcherConfig] = None,
    ):
        self.db = db
        self.config = config or RSSWatcherConfig()
        if not self.feed_url:
            raise ValueError(f"{type(self).__name__}.feed_url not set")

    # -----------------------------------------------------------------------
    # Single poll cycle
    # -----------------------------------------------------------------------

    async def poll_once(self) -> List[NewsEvent]:
        emitted: List[NewsEvent] = []
        error_msg: Optional[str] = None
        success = False

        try:
            entries = await self._fetch_feed()
            for entry in entries[: self.config.max_items_per_poll]:
                ev = self._entry_to_event(entry)
                if ev is None:
                    continue
                if self.db.insert_event(ev):
                    emitted.append(ev)
                    logger.info(
                        "%s new event: %s",
                        self.source_name, ev.headline[:80],
                    )
            success = True
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            logger.warning("%s poll error: %s", self.source_name, e)

        # Update source health (always)
        try:
            self.db.update_source_health(
                self.source_name,
                success=success,
                events_added=len(emitted),
                error=error_msg if not success else None,
            )
        except Exception as e:
            logger.warning("Failed to update source_health for %s: %s",
                           self.source_name, e)

        # Heartbeat
        try:
            self.db.pulse(self.source_name, {"events_added": len(emitted)})
        except Exception:
            pass

        return emitted

    # -----------------------------------------------------------------------
    # Feed fetching (uses feedparser via thread executor)
    # -----------------------------------------------------------------------

    async def _fetch_feed(self) -> List[dict]:
        """feedparser is sync; run in thread to avoid blocking event loop."""
        import feedparser  # lazy import — feedparser is optional at module-load

        loop = asyncio.get_running_loop()

        def _sync_parse():
            return feedparser.parse(
                self.feed_url,
                request_headers={"User-Agent": self.config.user_agent},
            )

        feed = await loop.run_in_executor(None, _sync_parse)
        if getattr(feed, "bozo", False):
            # feedparser sets bozo=1 on parse errors
            err = getattr(feed, "bozo_exception", "unknown")
            logger.debug("%s feedparser bozo: %s", self.source_name, err)

        entries = getattr(feed, "entries", []) or []
        return list(entries)

    # -----------------------------------------------------------------------
    # Entry → NewsEvent
    # -----------------------------------------------------------------------

    def _entry_to_event(self, entry: dict) -> Optional[NewsEvent]:
        """Convert feedparser entry dict → NewsEvent. None to skip."""
        headline = self._extract_str(entry, "title")
        if not headline:
            return None
        body = self._extract_str(entry, "summary") or self._extract_str(entry, "description") or ""
        link = self._extract_str(entry, "link") or self.feed_url
        published_at = self._parse_published(entry)

        # Dedup ID: prefer GUID/id, then link, then content hash
        guid = (
            self._extract_str(entry, "id")
            or self._extract_str(entry, "guid")
            or link
        )
        raw_id = f"{self.source_name}_{sha1(guid.encode()).hexdigest()[:16]}"

        now = datetime.now(timezone.utc)
        return NewsEvent(
            headline=self._clean(headline)[:500],
            body=self._clean(body)[:5000],
            source_url=link,
            source_domain=self.source_domain,
            source_tier=self.source_tier,
            published_at=published_at or now,
            fetched_at=now,
            raw_id=raw_id,
        )

    @staticmethod
    def _extract_str(entry: dict, key: str) -> str:
        val = entry.get(key)
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, dict):
            v = val.get("value") or val.get("href")
            return v.strip() if isinstance(v, str) else ""
        return ""

    @staticmethod
    def _parse_published(entry: dict) -> Optional[datetime]:
        # feedparser parses dates into 'published_parsed' (struct_time)
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t is not None:
                try:
                    import calendar
                    epoch = calendar.timegm(t)
                    return datetime.fromtimestamp(epoch, tz=timezone.utc)
                except Exception:
                    pass
        return None

    @staticmethod
    def _clean(text: str) -> str:
        """Strip HTML tags. No regex-heavy parsing — feedparser usually clean."""
        if not text:
            return ""
        import re
        # Strip tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # -----------------------------------------------------------------------
    # Background loop
    # -----------------------------------------------------------------------

    async def run_forever(self, on_event=None) -> None:
        logger.info(
            "%s starting: %ds interval", self.source_name,
            self.config.poll_interval_seconds,
        )
        while True:
            try:
                events = await self.poll_once()
                if on_event:
                    for ev in events:
                        try:
                            res = on_event(ev)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as e:
                            logger.exception("on_event handler raised: %s", e)
            except asyncio.CancelledError:
                logger.info("%s cancelled", self.source_name)
                break
            except Exception as e:
                logger.exception("%s poll cycle failed: %s", self.source_name, e)
            await asyncio.sleep(self.config.poll_interval_seconds)
