"""
QA Trade Trigger — Database Layer
==================================

SQLite-based persistence for:
  - Raw news events (deduplication via raw_id hash)
  - Classifications (heuristic + Claude L2 outputs)
  - Triggered signals (only those that passed all filter gates)
  - Filter audit log (which gate rejected what — for postmortem analysis)
  - Source health metrics (per-source success/failure stats)

Design follows existing project pattern (bot/core/pnl_ledger.py — SQLite via stdlib).
No external ORM (sqlalchemy etc.) — keep dependency surface minimal.

Path: data/trade_trigger.db (relative to project root)

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from .models import (
    NewsEvent, ClassificationResult, TradeSignal, AssetTrigger,
    Tier, Direction, TriggerVerdict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Raw news events. Dedup on raw_id (hash).
CREATE TABLE IF NOT EXISTS news_events (
    raw_id          TEXT PRIMARY KEY,
    headline        TEXT NOT NULL,
    body            TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    source_domain   TEXT NOT NULL,
    source_tier     TEXT NOT NULL,
    published_utc   TEXT NOT NULL,
    fetched_utc     TEXT NOT NULL,
    inserted_utc    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_events_published ON news_events(published_utc);
CREATE INDEX IF NOT EXISTS idx_events_domain ON news_events(source_domain);

-- Classifier outputs. One per event (last write wins on rerun).
CREATE TABLE IF NOT EXISTS classifications (
    raw_id              TEXT PRIMARY KEY,
    event_type          TEXT,
    actionable          INTEGER NOT NULL,
    actionability_score REAL NOT NULL,
    direction_hint      TEXT,
    asset_class_hint    TEXT,
    half_life_minutes   INTEGER NOT NULL DEFAULT 0,
    confidence          REAL NOT NULL,
    reasoning           TEXT NOT NULL,
    keywords_matched    TEXT NOT NULL DEFAULT '[]',
    classified_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    classifier_version  TEXT NOT NULL DEFAULT '0.1.0',
    FOREIGN KEY (raw_id) REFERENCES news_events(raw_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_class_event_type ON classifications(event_type);
CREATE INDEX IF NOT EXISTS idx_class_actionable ON classifications(actionable);

-- Trade signals (only those that passed ALL filter gates).
CREATE TABLE IF NOT EXISTS triggered_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id              TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    actionability_score REAL NOT NULL,
    reasoning           TEXT NOT NULL,
    sources_json        TEXT NOT NULL,
    triggers_json       TEXT NOT NULL,
    first_seen_utc      TEXT NOT NULL,
    fired_utc           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    user_action         TEXT,                       -- 'confirmed' / 'skipped' / NULL
    user_action_utc     TEXT,
    realized_outcome    TEXT,                       -- backtest fill: 'win' / 'loss' / 'flat'
    realized_pnl_pct    REAL,
    FOREIGN KEY (raw_id) REFERENCES news_events(raw_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_signals_fired ON triggered_signals(fired_utc);
CREATE INDEX IF NOT EXISTS idx_signals_event_type ON triggered_signals(event_type);

-- Filter audit log: which gate rejected what (for precision/recall postmortem).
CREATE TABLE IF NOT EXISTS filter_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id          TEXT NOT NULL,
    filter_name     TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    reason          TEXT,
    metadata_json   TEXT,
    checked_utc     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_raw_id ON filter_audit_log(raw_id);
CREATE INDEX IF NOT EXISTS idx_audit_filter ON filter_audit_log(filter_name);

-- Source health: per-source polling stats.
CREATE TABLE IF NOT EXISTS source_health (
    source_name     TEXT PRIMARY KEY,
    last_poll_utc   TEXT,
    last_success_utc TEXT,
    last_error      TEXT,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    total_polls     INTEGER NOT NULL DEFAULT 0,
    total_events    INTEGER NOT NULL DEFAULT 0
);

-- Heartbeat: bot pulse for monitoring.
CREATE TABLE IF NOT EXISTS heartbeat (
    component       TEXT PRIMARY KEY,
    last_pulse_utc  TEXT NOT NULL,
    metadata_json   TEXT
);

-- Polymarket odds history: tracks per-market outcome prices over time.
-- Used by sources.polymarket for shift detection (leading indicator).
CREATE TABLE IF NOT EXISTS polymarket_odds_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT NOT NULL,
    outcome_name    TEXT NOT NULL,
    price           REAL NOT NULL,           -- 0..1 (not cents)
    fetched_utc     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_pm_slug_time ON polymarket_odds_history(market_slug, fetched_utc DESC);
CREATE INDEX IF NOT EXISTS idx_pm_fetched ON polymarket_odds_history(fetched_utc);
"""


# ---------------------------------------------------------------------------
# Database wrapper
# ---------------------------------------------------------------------------

class TradeTriggerDB:
    """SQLite wrapper for trade_trigger module.

    Thread-safe via per-connection cursors (sqlite3 default).
    For multi-process access (bot_runner + classifier worker), WAL mode is enabled.
    """

    def __init__(self, db_path: str | Path = "data/trade_trigger.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        # PRAGMA foreign_keys is per-connection in SQLite — must enable each time
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA_SQL)
            c.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            c.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("initialized_utc", datetime.now(timezone.utc).isoformat()),
            )

    # -----------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------

    def insert_event(self, event: NewsEvent) -> bool:
        """Insert event. Returns True if new, False if dedup'd (already exists)."""
        with self._conn() as c:
            try:
                c.execute(
                    """INSERT INTO news_events
                       (raw_id, headline, body, source_url, source_domain,
                        source_tier, published_utc, fetched_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.raw_id, event.headline, event.body,
                        event.source_url, event.source_domain,
                        event.source_tier.value,
                        event.published_at.isoformat(),
                        event.fetched_at.isoformat(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False  # dedup

    def event_exists(self, raw_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM news_events WHERE raw_id = ? LIMIT 1", (raw_id,),
            ).fetchone()
            return row is not None

    def get_event(self, raw_id: str) -> Optional[NewsEvent]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM news_events WHERE raw_id = ?", (raw_id,),
            ).fetchone()
            if not row:
                return None
            return NewsEvent(
                headline=row["headline"],
                body=row["body"],
                source_url=row["source_url"],
                source_domain=row["source_domain"],
                source_tier=Tier(row["source_tier"]),
                published_at=datetime.fromisoformat(row["published_utc"]),
                fetched_at=datetime.fromisoformat(row["fetched_utc"]),
                raw_id=row["raw_id"],
            )

    def recent_events_for_topic(
        self,
        headline_substring: str,
        within_minutes: int = 15,
        now: Optional[datetime] = None,
    ) -> List[Tuple[str, str, str]]:
        """For corroboration gate: find recent events with substring in headline.
        Returns list of (raw_id, source_domain, published_utc).

        Pass `now` to override system clock (useful for testing & backtests).
        """
        from datetime import timedelta
        anchor = now or datetime.now(timezone.utc)
        cutoff_iso = (anchor - timedelta(minutes=within_minutes)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                """SELECT raw_id, source_domain, published_utc
                   FROM news_events
                   WHERE headline LIKE ?
                     AND published_utc >= ?
                   ORDER BY published_utc DESC""",
                (f"%{headline_substring}%", cutoff_iso),
            ).fetchall()
            return [(r["raw_id"], r["source_domain"], r["published_utc"]) for r in rows]

    # -----------------------------------------------------------------------
    # Classifications
    # -----------------------------------------------------------------------

    def upsert_classification(
        self, raw_id: str, result: ClassificationResult,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO classifications
                   (raw_id, event_type, actionable, actionability_score,
                    direction_hint, asset_class_hint, half_life_minutes,
                    confidence, reasoning, keywords_matched)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    raw_id,
                    result.event_type,
                    int(result.actionable),
                    result.actionability_score,
                    result.direction_hint.value if result.direction_hint else None,
                    result.asset_class_hint,
                    result.half_life_minutes,
                    result.confidence,
                    result.reasoning,
                    json.dumps(result.raw_keywords_matched, ensure_ascii=False),
                ),
            )

    # -----------------------------------------------------------------------
    # Triggered signals
    # -----------------------------------------------------------------------

    def insert_signal(self, raw_id: str, signal: TradeSignal) -> int:
        """Returns row id of inserted signal."""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO triggered_signals
                   (raw_id, event_type, verdict, actionability_score,
                    reasoning, sources_json, triggers_json, first_seen_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    raw_id,
                    signal.event_type,
                    signal.verdict.value,
                    signal.actionability_score,
                    signal.reasoning,
                    json.dumps(signal.sources, ensure_ascii=False),
                    json.dumps(
                        [
                            {
                                "ticker": t.ticker, "venue": t.venue,
                                "direction": t.direction.value,
                                "conviction": t.conviction,
                                "size_pct": t.suggested_size_pct_bucket,
                                "half_life_min": t.half_life_minutes,
                                "invalidation_price": t.invalidation_price,
                                "invalidation_reason": t.invalidation_reason,
                            }
                            for t in signal.triggers
                        ],
                        ensure_ascii=False,
                    ),
                    signal.first_seen_utc.isoformat(),
                ),
            )
            return cur.lastrowid

    def update_signal_user_action(
        self, signal_id: int, action: str,
    ) -> None:
        """Mark user reaction to alert: 'confirmed' / 'skipped'."""
        with self._conn() as c:
            c.execute(
                """UPDATE triggered_signals
                   SET user_action = ?,
                       user_action_utc = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id = ?""",
                (action, signal_id),
            )

    # -----------------------------------------------------------------------
    # Filter audit log
    # -----------------------------------------------------------------------

    def log_filter_check(
        self,
        raw_id: str,
        filter_name: str,
        passed: bool,
        reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO filter_audit_log
                   (raw_id, filter_name, passed, reason, metadata_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    raw_id, filter_name, int(passed), reason,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                ),
            )

    # -----------------------------------------------------------------------
    # Source health
    # -----------------------------------------------------------------------

    def update_source_health(
        self,
        source_name: str,
        success: bool,
        events_added: int = 0,
        error: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM source_health WHERE source_name = ?",
                (source_name,),
            ).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO source_health
                       (source_name, last_poll_utc, last_success_utc,
                        last_error, consecutive_fails, total_polls, total_events)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (
                        source_name, now,
                        now if success else None,
                        None if success else error,
                        0 if success else 1,
                        events_added,
                    ),
                )
            else:
                c.execute(
                    """UPDATE source_health SET
                       last_poll_utc = ?,
                       last_success_utc = COALESCE(?, last_success_utc),
                       last_error = ?,
                       consecutive_fails = ?,
                       total_polls = total_polls + 1,
                       total_events = total_events + ?
                       WHERE source_name = ?""",
                    (
                        now,
                        now if success else None,
                        None if success else error,
                        0 if success else row["consecutive_fails"] + 1,
                        events_added,
                        source_name,
                    ),
                )

    # -----------------------------------------------------------------------
    # Heartbeat
    # -----------------------------------------------------------------------

    def pulse(self, component: str, metadata: Optional[dict] = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO heartbeat
                   (component, last_pulse_utc, metadata_json)
                   VALUES (?, ?, ?)""",
                (
                    component,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                ),
            )

    # -----------------------------------------------------------------------
    # Read helpers for bot commands (/tt_audit, /tt_sources, /tt_recent)
    # -----------------------------------------------------------------------

    def get_signal_by_id(self, signal_id: int) -> Optional[dict]:
        """Return triggered signal as dict, joined with raw event headline."""
        with self._conn() as c:
            row = c.execute(
                """SELECT s.*, e.headline, e.source_domain
                   FROM triggered_signals s
                   LEFT JOIN news_events e ON e.raw_id = s.raw_id
                   WHERE s.id = ?""",
                (signal_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_audit_trail(self, raw_id: str) -> List[dict]:
        """Return all filter_audit_log rows for an event, oldest first."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT filter_name, passed, reason, metadata_json, checked_utc
                   FROM filter_audit_log
                   WHERE raw_id = ?
                   ORDER BY checked_utc ASC, id ASC""",
                (raw_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_signals(self, limit: int = 10) -> List[dict]:
        """Return N most recent triggered signals, newest first."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT s.id, s.event_type, s.actionability_score,
                          s.user_action, s.fired_utc,
                          e.headline, e.source_domain
                   FROM triggered_signals s
                   LEFT JOIN news_events e ON e.raw_id = s.raw_id
                   ORDER BY s.fired_utc DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def all_source_health(self) -> List[dict]:
        """Return health row for every registered source."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT source_name, last_poll_utc, last_success_utc,
                          last_error, consecutive_fails, total_polls, total_events
                   FROM source_health
                   ORDER BY source_name"""
            ).fetchall()
            return [dict(r) for r in rows]

    def heartbeat_status(self) -> List[dict]:
        """All component heartbeats for /tt_status verbose."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT component, last_pulse_utc, metadata_json FROM heartbeat"
            ).fetchall()
            return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Polymarket odds history
    # -----------------------------------------------------------------------

    def insert_polymarket_odds(
        self, market_slug: str, outcome_name: str, price: float,
    ) -> None:
        """Append one odds observation. Append-only (no update)."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO polymarket_odds_history
                   (market_slug, outcome_name, price)
                   VALUES (?, ?, ?)""",
                (market_slug, outcome_name, price),
            )

    def latest_polymarket_odds(
        self, market_slug: str, outcome_name: str,
    ) -> Optional[Tuple[float, datetime]]:
        """Return (price, fetched_utc) of most recent observation, or None."""
        with self._conn() as c:
            row = c.execute(
                """SELECT price, fetched_utc
                   FROM polymarket_odds_history
                   WHERE market_slug = ? AND outcome_name = ?
                   ORDER BY fetched_utc DESC LIMIT 1""",
                (market_slug, outcome_name),
            ).fetchone()
            if not row:
                return None
            return (float(row["price"]), datetime.fromisoformat(row["fetched_utc"]))

    def polymarket_odds_window(
        self,
        market_slug: str,
        outcome_name: str,
        within_minutes: int,
        now: Optional[datetime] = None,
    ) -> List[Tuple[float, datetime]]:
        """Return list of (price, fetched_utc) for observations in the window.
        Newest first.
        """
        from datetime import timedelta
        anchor = now or datetime.now(timezone.utc)
        cutoff_iso = (anchor - timedelta(minutes=within_minutes)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                """SELECT price, fetched_utc
                   FROM polymarket_odds_history
                   WHERE market_slug = ?
                     AND outcome_name = ?
                     AND fetched_utc >= ?
                   ORDER BY fetched_utc DESC""",
                (market_slug, outcome_name, cutoff_iso),
            ).fetchall()
            return [
                (float(r["price"]), datetime.fromisoformat(r["fetched_utc"]))
                for r in rows
            ]

    def prune_polymarket_history(self, keep_days: int = 7) -> int:
        """Delete observations older than keep_days. Returns rows deleted."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM polymarket_odds_history WHERE fetched_utc < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            return cur.rowcount

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    def stats(self) -> dict:
        """Quick stats for /tt_status command."""
        with self._conn() as c:
            return {
                "total_events": c.execute("SELECT COUNT(*) FROM news_events").fetchone()[0],
                "events_24h": c.execute(
                    "SELECT COUNT(*) FROM news_events WHERE published_utc >= datetime('now', '-1 day')"
                ).fetchone()[0],
                "total_signals": c.execute("SELECT COUNT(*) FROM triggered_signals").fetchone()[0],
                "signals_24h": c.execute(
                    "SELECT COUNT(*) FROM triggered_signals WHERE fired_utc >= datetime('now', '-1 day')"
                ).fetchone()[0],
                "actionable_classifications": c.execute(
                    "SELECT COUNT(*) FROM classifications WHERE actionable = 1"
                ).fetchone()[0],
                "sources_count": c.execute("SELECT COUNT(*) FROM source_health").fetchone()[0],
                "sources_healthy": c.execute(
                    "SELECT COUNT(*) FROM source_health WHERE consecutive_fails = 0"
                ).fetchone()[0],
            }
