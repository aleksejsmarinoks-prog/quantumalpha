"""Equity DB auto-create tests (Phase 7.2 Issue 2)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.core._equity_db_helper import (
    ensure_equity_db,
    insert_equity_snapshot,
)


class TestEnsureEquityDB:

    def test_creates_db_when_missing(self, tmp_path):
        db_path = tmp_path / "subdir" / "equity.db"
        assert not db_path.exists()

        result = ensure_equity_db(db_path)

        assert result == db_path
        assert db_path.exists()
        # Parent dir auto-created
        assert db_path.parent.is_dir()

    def test_creates_tables(self, tmp_path):
        db_path = tmp_path / "equity.db"
        ensure_equity_db(db_path)

        conn = sqlite3.connect(db_path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()

        assert "equity_snapshots" in tables

    def test_idempotent_on_existing_db(self, tmp_path):
        """Calling twice does not raise or wipe data."""
        db_path = tmp_path / "equity.db"
        ensure_equity_db(db_path)

        # Insert a row
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO equity_snapshots (snapshot_utc, equity) VALUES (?, ?)",
            ("2026-05-13T10:00:00Z", 1000.0),
        )
        conn.commit()
        conn.close()

        # Call again — should NOT clobber
        ensure_equity_db(db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        conn.close()
        assert count == 1

    def test_accepts_string_path(self, tmp_path):
        db_path = tmp_path / "equity.db"
        result = ensure_equity_db(str(db_path))
        assert isinstance(result, Path)
        assert result.exists()

    def test_schema_indices_created(self, tmp_path):
        db_path = tmp_path / "equity.db"
        ensure_equity_db(db_path)

        conn = sqlite3.connect(db_path)
        indices = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        conn.close()
        assert "idx_equity_snapshot_utc" in indices


class TestInsertEquitySnapshot:

    def test_insert_creates_db_if_missing(self, tmp_path):
        """Phase 7.2 Issue 2 core: first snapshot ever should auto-create DB."""
        db_path = tmp_path / "fresh.db"
        assert not db_path.exists()

        row_id = insert_equity_snapshot(
            db_path,
            snapshot_utc="2026-05-13T10:00:00Z",
            equity=1000.0,
            open_positions=0,
        )

        assert row_id > 0
        assert db_path.exists()

        # Verify row
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT equity, open_positions FROM equity_snapshots WHERE id = ?",
            (row_id,),
        ).fetchone()
        conn.close()
        assert row == (1000.0, 0)

    def test_multiple_inserts_preserve_history(self, tmp_path):
        db_path = tmp_path / "equity.db"
        for i, equity in enumerate([1000.0, 1001.5, 999.2, 1003.0]):
            insert_equity_snapshot(
                db_path,
                snapshot_utc=f"2026-05-13T{10+i:02d}:00:00Z",
                equity=equity,
            )

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        conn.close()
        assert count == 4

    def test_concurrent_inserts_dont_crash(self, tmp_path):
        """Two threads inserting simultaneously should both succeed."""
        db_path = tmp_path / "equity.db"
        ensure_equity_db(db_path)
        errors = []

        def worker(tag):
            try:
                for i in range(5):
                    insert_equity_snapshot(
                        db_path,
                        snapshot_utc=f"2026-05-13T10:0{i}:00Z",
                        equity=1000.0 + i,
                    )
            except Exception as e:
                errors.append(f"{tag}: {e}")

        threads = [
            threading.Thread(target=worker, args=("A",)),
            threading.Thread(target=worker, args=("B",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent inserts crashed: {errors}"
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        conn.close()
        assert count == 10
