"""
core/funding_monitor.py — QuantumAlpha Funding Rate Monitor v1.0

Background async scanner for Bybit USDT perpetual funding rates.
Runs every N minutes, stores history, emits opportunity signals.

Why this exists:
  - Funding rates change every 8h on Bybit but vary widely.
  - We need baseline distribution data BEFORE calibrating funding_arb thresholds.
  - 7-14 days of monitoring gives us percentile distribution to set entries.
  - Public endpoint, NO API keys required.

Storage: separate SQLite DB at data/funding_history.db (independent from PnL ledger).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Awaitable

from .bybit_client import BybitClient, FundingRate

log = logging.getLogger("qa_bot.funding_monitor")


# =============================================================================
# CONFIGURATION
# =============================================================================

# Default symbols to monitor — our prop trading universe
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Polling interval — funding rates only change every 8h, but we sample more
# often to catch intra-hour rate forecasts that Bybit publishes.
DEFAULT_POLL_INTERVAL_SEC = 300   # 5 min

# Opportunity signal thresholds (per-8h rates)
SIGNAL_HIGH_POSITIVE = 0.0005     # > 0.05% per 8h = ~54% APR (strong long-pay-short opportunity)
SIGNAL_HIGH_NEGATIVE = -0.0005    # < -0.05% per 8h = ~-54% APR (strong short-pay-long)
SIGNAL_EXTREME       = 0.0010     # 0.10% per 8h = ~109% APR (rare)


# =============================================================================
# SCHEMA
# =============================================================================

SCHEMA_SQL = """
-- Time-series funding rate snapshots
CREATE TABLE IF NOT EXISTS funding_rate_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_utc         TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    funding_rate        REAL    NOT NULL,           -- per 8h, e.g. 0.0001 = 0.01%
    next_funding_time_ms INTEGER,
    annualized_pct      REAL    NOT NULL,           -- pre-computed for quick queries
    last_price          REAL,                        -- ticker last_price at sample time
    UNIQUE(symbol, next_funding_time_ms)            -- dedupe per settlement
);
CREATE INDEX IF NOT EXISTS idx_fr_symbol ON funding_rate_history(symbol);
CREATE INDEX IF NOT EXISTS idx_fr_fetched ON funding_rate_history(fetched_utc);

-- Opportunity events log (when threshold crossed)
CREATE TABLE IF NOT EXISTS funding_opportunities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_utc    TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    funding_rate    REAL    NOT NULL,
    annualized_pct  REAL    NOT NULL,
    severity        TEXT    NOT NULL,                -- 'HIGH' | 'EXTREME'
    direction       TEXT    NOT NULL,                -- 'LONG_GETS_PAID' | 'SHORT_GETS_PAID'
    notified        INTEGER NOT NULL DEFAULT 0       -- 0 = not yet sent to Telegram
);
CREATE INDEX IF NOT EXISTS idx_opp_notified ON funding_opportunities(notified);
"""


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class FundingStats:
    """Aggregated statistics for a symbol over a time window."""
    symbol:           str
    samples:          int
    mean_rate:        float
    median_rate:      float
    p25:              float
    p75:              float
    p95:              float
    max_rate:         float
    min_rate:         float
    pct_above_005:    float    # frequency of rate > 0.05% per 8h
    pct_above_010:    float    # frequency of rate > 0.10% per 8h
    pct_below_neg005: float    # frequency of rate < -0.05%

    def to_telegram(self) -> str:
        return (
            f"📊 *{self.symbol} Funding Stats* ({self.samples} samples)\n"
            f"  Mean: `{self.mean_rate*100:+.4f}%/8h`  "
            f"Median: `{self.median_rate*100:+.4f}%/8h`\n"
            f"  P25/P75: `{self.p25*100:+.4f}` / `{self.p75*100:+.4f}`\n"
            f"  P95: `{self.p95*100:+.4f}`  Max: `{self.max_rate*100:+.4f}`\n"
            f"  Above 0.05%: `{self.pct_above_005:.1f}%` of samples\n"
            f"  Above 0.10%: `{self.pct_above_010:.1f}%` of samples"
        )


@dataclass
class OpportunityEvent:
    detected_utc:   str
    symbol:         str
    funding_rate:   float
    annualized_pct: float
    severity:       str           # 'HIGH' | 'EXTREME'
    direction:      str           # 'LONG_GETS_PAID' | 'SHORT_GETS_PAID'

    def to_telegram(self) -> str:
        icon = "🔥" if self.severity == "EXTREME" else "⚡"
        return (
            f"{icon} *Funding Opportunity* — `{self.symbol}`\n"
            f"Rate: `{self.funding_rate*100:+.4f}%/8h` "
            f"= `{self.annualized_pct:+.2f}% APR`\n"
            f"Direction: `{self.direction}`\n"
            f"Severity: `{self.severity}`\n"
            f"`{self.detected_utc[:19]}`"
        )


# =============================================================================
# FUNDING MONITOR
# =============================================================================

class FundingMonitor:
    """
    Async background monitor for funding rates.

    Usage:
        monitor = FundingMonitor(db_path=Path("data/funding_history.db"))
        await monitor.start()  # runs forever in asyncio task
        # ... later
        monitor.stop()

    With opportunity callback:
        async def alert_telegram(event: OpportunityEvent):
            await bot.send_message(USER_ID, event.to_telegram())

        monitor = FundingMonitor(db_path=..., opportunity_callback=alert_telegram)
    """

    def __init__(
        self,
        db_path:                Path,
        symbols:                list[str] = None,
        poll_interval_sec:      int = DEFAULT_POLL_INTERVAL_SEC,
        opportunity_callback:   Optional[Callable[[OpportunityEvent], Awaitable[None]]] = None,
    ):
        self.db_path  = db_path
        self.symbols  = symbols or DEFAULT_SYMBOLS
        self.interval = poll_interval_sec
        self._opp_cb  = opportunity_callback
        self._task:   Optional[asyncio.Task] = None
        self._stop:   asyncio.Event = asyncio.Event()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA_SQL)
        log.info(f"FundingMonitor DB ready at {self.db_path}")

    # ── PUBLIC API ──────────────────────────────────────────────────────────────

    async def start(self):
        """Start background polling task."""
        if self._task is not None:
            log.warning("FundingMonitor already running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="funding_monitor_loop")
        log.info(
            f"FundingMonitor started: symbols={self.symbols} "
            f"interval={self.interval}s"
        )

    async def stop(self):
        """Stop background task gracefully."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        log.info("FundingMonitor stopped")

    async def fetch_once(self) -> list[FundingRate]:
        """
        Single fetch pass — useful for manual /funding command.
        Returns latest funding rates and stores them.
        """
        async with BybitClient() as client:
            results = []
            for symbol in self.symbols:
                try:
                    fr = await client.fetch_funding_rate(symbol)
                    self._store_rate(fr, last_price=None)
                    results.append(fr)
                    await self._maybe_emit_opportunity(fr)
                except Exception as e:
                    log.warning(f"Failed to fetch {symbol}: {e}")
            return results

    def get_latest_rates(self) -> list[dict]:
        """Latest stored rate per symbol — for /funding command."""
        with self._conn() as c:
            cur = c.execute("""
                SELECT symbol, funding_rate, annualized_pct, fetched_utc
                FROM funding_rate_history
                WHERE id IN (
                    SELECT MAX(id) FROM funding_rate_history GROUP BY symbol
                )
                ORDER BY symbol
            """)
            return [dict(r) for r in cur.fetchall()]

    def get_stats(self, symbol: str, days: int = 7) -> Optional[FundingStats]:
        """
        Aggregated stats for a symbol over last N days.
        Returns None if not enough data (< 10 samples).
        """
        with self._conn() as c:
            cur = c.execute("""
                SELECT funding_rate FROM funding_rate_history
                WHERE symbol = ? AND fetched_utc >= datetime('now', ?)
                ORDER BY fetched_utc
            """, (symbol, f"-{days} days"))
            rates = [r["funding_rate"] for r in cur.fetchall()]

        if len(rates) < 10:
            return None

        rates_sorted = sorted(rates)
        n = len(rates_sorted)

        def pct(p: float) -> float:
            idx = max(0, min(n - 1, int(n * p)))
            return rates_sorted[idx]

        pct_above_005    = sum(1 for r in rates if r > 0.0005)  / n * 100
        pct_above_010    = sum(1 for r in rates if r > 0.0010)  / n * 100
        pct_below_neg005 = sum(1 for r in rates if r < -0.0005) / n * 100

        return FundingStats(
            symbol=symbol,
            samples=n,
            mean_rate=sum(rates) / n,
            median_rate=pct(0.50),
            p25=pct(0.25),
            p75=pct(0.75),
            p95=pct(0.95),
            max_rate=max(rates),
            min_rate=min(rates),
            pct_above_005=pct_above_005,
            pct_above_010=pct_above_010,
            pct_below_neg005=pct_below_neg005,
        )

    def get_unnotified_opportunities(self, limit: int = 10) -> list[dict]:
        """Pending opportunity events for Telegram dispatch."""
        with self._conn() as c:
            cur = c.execute("""
                SELECT * FROM funding_opportunities
                WHERE notified = 0
                ORDER BY detected_utc ASC LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def mark_opportunity_notified(self, opp_id: int):
        with self._conn() as c:
            c.execute(
                "UPDATE funding_opportunities SET notified = 1 WHERE id = ?",
                (opp_id,)
            )

    # ── INTERNAL ────────────────────────────────────────────────────────────────

    async def _run_loop(self):
        """Main polling loop. Runs until _stop event set."""
        async with BybitClient() as client:
            while not self._stop.is_set():
                try:
                    for symbol in self.symbols:
                        if self._stop.is_set():
                            break
                        try:
                            fr = await client.fetch_funding_rate(symbol)
                            # Try to grab last_price too (best-effort)
                            try:
                                ticker = await client.fetch_ticker(symbol)
                                last_price = ticker.last_price
                            except Exception:
                                last_price = None
                            self._store_rate(fr, last_price)
                            await self._maybe_emit_opportunity(fr)
                        except Exception as e:
                            log.warning(f"[{symbol}] fetch error: {e}")
                except Exception as e:
                    log.error(f"Loop iteration error: {e}")

                # Wait for next interval or stop signal
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass  # Normal timeout, continue loop

    def _store_rate(self, fr: FundingRate, last_price: Optional[float]):
        with self._conn() as c:
            try:
                c.execute("""
                    INSERT OR IGNORE INTO funding_rate_history (
                        fetched_utc, symbol, funding_rate, next_funding_time_ms,
                        annualized_pct, last_price
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    datetime.fromtimestamp(fr.fetched_at_utc, tz=timezone.utc).isoformat(),
                    fr.symbol, fr.funding_rate, fr.next_funding_time_ms,
                    fr.annualized_pct, last_price,
                ))
            except sqlite3.IntegrityError:
                pass  # Already have this settlement

    async def _maybe_emit_opportunity(self, fr: FundingRate):
        """Check if rate crosses thresholds → store + (optionally) callback."""
        rate = fr.funding_rate
        severity = None
        direction = None

        if rate >= SIGNAL_EXTREME:
            severity, direction = "EXTREME", "LONG_GETS_PAID"
            # Wait — extreme positive funding means longs PAY shorts.
            # So shorts get paid. Let me fix:
            severity, direction = "EXTREME", "SHORT_GETS_PAID"
        elif rate <= -SIGNAL_EXTREME:
            severity, direction = "EXTREME", "LONG_GETS_PAID"
        elif rate >= SIGNAL_HIGH_POSITIVE:
            severity, direction = "HIGH", "SHORT_GETS_PAID"
        elif rate <= SIGNAL_HIGH_NEGATIVE:
            severity, direction = "HIGH", "LONG_GETS_PAID"

        if severity is None:
            return

        # Check we haven't already alerted for this settlement
        with self._conn() as c:
            cur = c.execute("""
                SELECT id FROM funding_opportunities
                WHERE symbol = ? AND detected_utc >= datetime('now', '-1 hour')
                LIMIT 1
            """, (fr.symbol,))
            if cur.fetchone():
                return  # Already alerted recently

            event = OpportunityEvent(
                detected_utc=datetime.now(timezone.utc).isoformat(),
                symbol=fr.symbol,
                funding_rate=fr.funding_rate,
                annualized_pct=fr.annualized_pct,
                severity=severity,
                direction=direction,
            )
            c.execute("""
                INSERT INTO funding_opportunities (
                    detected_utc, symbol, funding_rate,
                    annualized_pct, severity, direction
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event.detected_utc, event.symbol, event.funding_rate,
                event.annualized_pct, event.severity, event.direction,
            ))

        log.info(
            f"OPPORTUNITY: {fr.symbol} {severity} {direction} "
            f"rate={fr.funding_rate*100:+.4f}%/8h ({fr.annualized_pct:+.0f}% APR)"
        )

        if self._opp_cb:
            try:
                await self._opp_cb(event)
            except Exception as e:
                log.error(f"Opportunity callback error: {e}")


# =============================================================================
# CLI / TEST
# =============================================================================

async def _smoke_test():
    """Single fetch pass to verify connectivity + storage."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test_funding.db"
        monitor = FundingMonitor(db_path=db, poll_interval_sec=60)

        print(f"\n{'='*70}")
        print(f"FundingMonitor smoke test — DB: {db}")
        print(f"{'='*70}\n")

        results = await monitor.fetch_once()

        print(f"\nFetched {len(results)} symbols:")
        for fr in results:
            print(
                f"  {fr.symbol:10s}  rate={fr.funding_rate*100:+.4f}%/8h  "
                f"APR={fr.annualized_pct:+.2f}%"
            )

        print("\nStored in DB:")
        for r in monitor.get_latest_rates():
            print(f"  {r['symbol']}  rate={r['funding_rate']*100:+.4f}%/8h "
                  f"APR={r['annualized_pct']:+.2f}%  fetched={r['fetched_utc'][:19]}")

        # Stats won't return anything yet (need >10 samples)
        stats = monitor.get_stats("ETHUSDT", days=7)
        if stats:
            print(f"\n{stats.to_telegram()}")
        else:
            print("\n  (Stats unavailable — need 7+ days of data)")

        opps = monitor.get_unnotified_opportunities()
        if opps:
            print(f"\n{len(opps)} opportunity events pending notification:")
            for o in opps:
                print(f"  - {o['symbol']} {o['severity']} {o['direction']}")
        else:
            print("\n  No opportunity events triggered (rates within normal range)")

        print(f"\n{'='*70}")
        print("✅ Smoke test complete")
        print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
