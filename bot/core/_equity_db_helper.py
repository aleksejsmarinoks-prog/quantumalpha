"""
QA Core — Equity DB Helper (Phase 7.2)
========================================

Self-contained helper that ensures `data/equity.db` exists with proper schema.
Designed to be called from `pnl_ledger.snapshot_equity()` to auto-create the
database on first use (fixes Phase 7.1 Issue 2: equity tracking offline).

Usage from pnl_ledger.py (one-line addition):

    from bot.core._equity_db_helper import ensure_equity_db

    def snapshot_equity(self, ...):
        ensure_equity_db(self.db_path)  # idempotent, safe on every call
        # ... existing snapshot logic ...

The helper is intentionally tiny and side-effect-only. It does NOT replace
PnLLedger; it just guarantees the DB exists.

Schema:
  equity_snapshots(
      id              INTEGER PRIMARY KEY,
      snapshot_utc    TEXT NOT NULL,
      equity          REAL NOT NULL,
      open_positions  INTEGER DEFAULT 0,
      realized_pnl    REAL DEFAULT 0,
      unrealized_pnl  REAL DEFAULT 0
  )

Author: QuantumAlpha
Version: 7.2.0
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Union

logger = logging.getLogger("qa.core.equity_db")


_EQUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_utc    TEXT NOT NULL,
    equity          REAL NOT NULL,
    open_positions  INTEGER DEFAULT 0,
    realized_pnl    REAL DEFAULT 0,
    unrealized_pnl  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_snapshot_utc
    ON equity_snapshots(snapshot_utc);
"""


def ensure_equity_db(db_path: Union[str, Path]) -> Path:
    """Idempotently create equity DB file + tables. Returns Path.

    Safe to call on every snapshot. Auto-creates parent directories.
    Uses WAL mode to play nice with the digest reader (read-only at same time).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=5.0)
    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(_EQUITY_SCHEMA)
        conn.commit()
    finally:
        conn.close()

    return path


def insert_equity_snapshot(
    db_path: Union[str, Path],
    snapshot_utc: str,
    equity: float,
    open_positions: int = 0,
    realized_pnl: float = 0.0,
    unrealized_pnl: float = 0.0,
) -> int:
    """Insert a single equity snapshot row. Returns new row id.

    Auto-creates DB via ensure_equity_db. Designed to be called once per hour
    by the scheduler `_equity_snapshot` job.
    """
    path = ensure_equity_db(db_path)
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        cursor = conn.execute(
            "INSERT INTO equity_snapshots "
            "(snapshot_utc, equity, open_positions, realized_pnl, unrealized_pnl) "
            "VALUES (?, ?, ?, ?, ?)",
            (snapshot_utc, float(equity), int(open_positions),
             float(realized_pnl), float(unrealized_pnl)),
        )
        conn.commit()
        row_id = cursor.lastrowid or 0
        logger.info(
            "equity snapshot inserted id=%d equity=%.2f open_pos=%d",
            row_id, equity, open_positions,
        )
        return row_id
    finally:
        conn.close()
