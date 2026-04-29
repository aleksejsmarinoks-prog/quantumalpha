"""
QuantumAlpha — Macro Events Detector
=====================================

Polls external sources for events that should trigger DCAdips strategy:
  - VIX > 30 (threshold-based)
  - Fed rate change announcements
  - CRITICAL geopolitical events (from Diplomatic Feed when integrated)

Async polling with cool-down between alerts to avoid duplicate firing.

Data sources:
    - VIX: Yahoo Finance via yfinance (15-min delay acceptable for macro)
    - Fed: FOMC calendar (hard-coded scheduled dates) + post-meeting RSS
    - Geopolitical: Diplomatic Feed integration (Signal #45 — separate module)

Important:
    During fast-moving regimes (active S13 / VIX > 25), this poller switches
    to 5-min interval. Default is 30-min poll.

Version: 1.0 (commit #004)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from bot.strategies.dca_dips import MacroEvent, MacroEventType


logger = logging.getLogger("qa.macro_events")


# ---- Tunable parameters ----
VIX_TRIGGER_THRESHOLD = 30.0
VIX_RESET_THRESHOLD = 22.0                    # must drop below this before new VIX trigger
DEFAULT_POLL_INTERVAL_SEC = 1800              # 30 min
HIGH_VOL_POLL_INTERVAL_SEC = 300              # 5 min when VIX > 25

# Pre-known Fed FOMC meeting dates (2026)
# Update annually; this list controls when bot expects a Fed announcement
FOMC_2026_DATES = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",   # this week!
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-11-04",
    "2026-12-16",
]


@dataclass
class VIXReading:
    value: float
    fetched_at: datetime
    source: str


@dataclass
class FedEvent:
    meeting_date: str             # "YYYY-MM-DD"
    rate_change_bps: Optional[int]
    statement_summary: Optional[str]
    detected_at: datetime


class MacroEventDetector:
    """
    Async polling detector. Calls registered callbacks when events fire.

    Usage:
        detector = MacroEventDetector(vix_fetcher=my_vix_fn)
        detector.on_event(my_dca_strategy.trigger_event)
        await detector.start()  # runs forever
    """

    def __init__(
        self,
        vix_fetcher: Optional[Callable[[], Awaitable[Optional[float]]]] = None,
        fed_fetcher: Optional[Callable[[str], Awaitable[Optional[FedEvent]]]] = None,
    ):
        self._vix_fetcher = vix_fetcher
        self._fed_fetcher = fed_fetcher

        self._callbacks: List[Callable[[MacroEvent], bool]] = []
        self._last_vix_value: Optional[float] = None
        self._vix_above_threshold_since: Optional[datetime] = None
        self._last_vix_event_at: Optional[datetime] = None
        self._fed_events_fired: set = set()    # meeting dates already triggered
        self._stopped = False
        self._poll_interval = DEFAULT_POLL_INTERVAL_SEC

    def on_event(self, callback: Callable[[MacroEvent], bool]) -> None:
        """Register a callback. Returns True if event was accepted."""
        self._callbacks.append(callback)

    def _emit(self, event: MacroEvent) -> int:
        """Call all callbacks; return count of successful registrations."""
        ok_count = 0
        for cb in self._callbacks:
            try:
                if cb(event):
                    ok_count += 1
            except Exception as e:
                logger.exception("Callback failed for event %s: %s", event.event_id, e)
        logger.info(
            "Macro event %s emitted to %d/%d callbacks",
            event.event_id, ok_count, len(self._callbacks)
        )
        return ok_count

    # ---- VIX detection ----
    async def _check_vix(self) -> None:
        if self._vix_fetcher is None:
            return

        try:
            vix = await self._vix_fetcher()
        except Exception as e:
            logger.warning("VIX fetch failed: %s", e)
            return

        if vix is None:
            return

        self._last_vix_value = vix
        now = datetime.now(timezone.utc)

        # Adjust polling cadence based on volatility
        if vix > 25:
            self._poll_interval = HIGH_VOL_POLL_INTERVAL_SEC
        else:
            self._poll_interval = DEFAULT_POLL_INTERVAL_SEC

        # Trigger logic with hysteresis (prevents flickering around threshold)
        if vix >= VIX_TRIGGER_THRESHOLD:
            if self._vix_above_threshold_since is None:
                self._vix_above_threshold_since = now

            # Only fire if VIX has been above threshold for >= 30 min sustained
            duration = (now - self._vix_above_threshold_since).total_seconds()
            if duration >= 1800:
                # Don't refire within 24h
                if (
                    self._last_vix_event_at is None
                    or (now - self._last_vix_event_at) >= timedelta(hours=24)
                ):
                    severity = min((vix - VIX_TRIGGER_THRESHOLD) / 20.0 + 0.5, 1.0)
                    event = self._build_event(
                        event_type=MacroEventType.VIX_SPIKE,
                        description=f"VIX spike: {vix:.2f} (threshold {VIX_TRIGGER_THRESHOLD})",
                        severity=severity,
                        sources=["yahoo_finance"],
                        corroborated=True,                # VIX is single authoritative source
                    )
                    self._emit(event)
                    self._last_vix_event_at = now

        elif vix <= VIX_RESET_THRESHOLD:
            # Reset trigger state when VIX falls back to calm
            self._vix_above_threshold_since = None

    # ---- Fed event detection ----
    async def _check_fed(self) -> None:
        if self._fed_fetcher is None:
            return

        # Check if today is a known FOMC date
        today = datetime.now(timezone.utc).date().isoformat()
        if today not in FOMC_2026_DATES:
            return

        if today in self._fed_events_fired:
            return

        try:
            fed_event = await self._fed_fetcher(today)
        except Exception as e:
            logger.warning("Fed fetcher failed for %s: %s", today, e)
            return

        if fed_event is None:
            return

        # Severity scales with rate change magnitude
        rate_bps = fed_event.rate_change_bps or 0
        if abs(rate_bps) < 25:
            severity = 0.4
        elif abs(rate_bps) <= 50:
            severity = 0.7
        else:
            severity = 0.95

        event = self._build_event(
            event_type=MacroEventType.FED_RATE_CHANGE,
            description=f"FOMC: {rate_bps:+d}bps | {fed_event.statement_summary or 'no summary'}",
            severity=severity,
            sources=["federalreserve.gov"],
            corroborated=True,
        )
        self._emit(event)
        self._fed_events_fired.add(today)

    # ---- Geopolitical CRITICAL ----
    def trigger_geopolitical(
        self,
        description: str,
        severity: float,
        sources: List[str],
    ) -> bool:
        """
        Manual trigger for geopolitical CRITICAL events.

        In production this is called by Diplomatic Feed (Signal #45) when an
        impact_score >= 0.85 article is detected from 2+ Tier-1 sources.

        Returns True if any DCA strategy accepted the trigger.
        """
        corroborated = len(sources) >= 2
        event = self._build_event(
            event_type=MacroEventType.GEOPOLITICAL_CRITICAL,
            description=description,
            severity=max(0.0, min(severity, 1.0)),
            sources=sources,
            corroborated=corroborated,
        )
        accepted = self._emit(event)
        return accepted > 0

    # ---- common helpers ----
    def _build_event(
        self,
        event_type: MacroEventType,
        description: str,
        severity: float,
        sources: List[str],
        corroborated: bool,
    ) -> MacroEvent:
        now = datetime.now(timezone.utc)
        # Stable event ID derived from type + day + description (avoid duplicates)
        seed = f"{event_type.value}|{now.date().isoformat()}|{description[:64]}"
        event_id = hashlib.sha256(seed.encode()).hexdigest()[:16]
        return MacroEvent(
            event_id=event_id,
            event_type=event_type,
            triggered_at=now,
            description=description,
            severity=severity,
            sources=sources,
            corroborated=corroborated,
        )

    # ---- main loop ----
    async def start(self) -> None:
        """Run forever, polling at adaptive cadence."""
        logger.info("MacroEventDetector started")
        while not self._stopped:
            try:
                await self._check_vix()
                await self._check_fed()
            except Exception as e:
                logger.exception("Macro detector loop iteration failed: %s", e)

            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._stopped = True

    def get_status(self) -> Dict[str, Any]:
        return {
            "last_vix_value": self._last_vix_value,
            "vix_above_threshold_since": (
                self._vix_above_threshold_since.isoformat()
                if self._vix_above_threshold_since else None
            ),
            "last_vix_event_at": (
                self._last_vix_event_at.isoformat()
                if self._last_vix_event_at else None
            ),
            "fed_events_fired_count": len(self._fed_events_fired),
            "current_poll_interval_sec": self._poll_interval,
            "callbacks_registered": len(self._callbacks),
        }


# ---- default VIX fetcher using yfinance ----
async def default_vix_fetcher() -> Optional[float]:
    """
    Fetch latest VIX close. Best-effort — returns None on any failure.
    yfinance is sync, run in executor.
    """
    try:
        import yfinance as yf

        loop = asyncio.get_running_loop()

        def _fetch() -> Optional[float]:
            try:
                vix = yf.Ticker("^VIX")
                hist = vix.history(period="1d", interval="5m")
                if hist.empty:
                    return None
                return float(hist["Close"].iloc[-1])
            except Exception:
                return None

        return await loop.run_in_executor(None, _fetch)
    except ImportError:
        logger.warning("yfinance not installed; VIX fetcher disabled")
        return None
    except Exception as e:
        logger.warning("Default VIX fetcher failed: %s", e)
        return None


# ---- self-test ----
if __name__ == "__main__":
    async def main():
        logging.basicConfig(level=logging.INFO)

        detector = MacroEventDetector(vix_fetcher=default_vix_fetcher)

        events_received = []
        def cb(event: MacroEvent) -> bool:
            events_received.append(event)
            print(f"EVENT FIRED: {event.event_type.value} | sev={event.severity:.2f} | {event.description}")
            return True

        detector.on_event(cb)

        # Test geopolitical manual trigger
        ok = detector.trigger_geopolitical(
            description="Hormuz Strait closure announcement",
            severity=0.92,
            sources=["reuters", "bloomberg"],
        )
        print(f"Geopolitical trigger accepted: {ok}")

        # Status
        import json
        print(json.dumps(detector.get_status(), indent=2))

    asyncio.run(main())
