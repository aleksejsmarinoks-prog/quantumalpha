r"""
QA Daily Digest — Data Aggregator (Phase 7.2 — Housekeeping)
==============================================================

Changes vs Phase 7.1:
  * Issue 1: Strategy activity counts now use STRICT [EVAL_TICK] markers only.
             Old broad regex (e.g. r"\blv1\b.*\beval\b") replaced with
             r"\[EVAL_TICK\]\s+strategy=(\S+)" — counts ONE per evaluation cycle.
             No backwards compat: pre-Phase 7.2 logs will show 0 for strategies.
  * Issue 4: Proper 24h log windowing with timestamp inheritance.
             Lines without timestamps now inherit from the previous timestamped
             line (correct multi-line/stacktrace handling). Lines whose
             inherited timestamp is before cutoff are SKIPPED, not counted.
  * Issue 4b: Error/Warning counts now broken out by severity:
             {"CRITICAL": N, "ERROR": N, "WARNING": N}

Public surface preserved — same function names, same return shape EXCEPT:
  - aggregate_log_events now returns `eval_ticks_by_strategy` instead of
    `lv1_evals` / `funding_cycles` / `mean_rev_signals`
  - aggregate_log_events now returns `severity_counts` instead of
    flat `errors` / `warnings` keys (these still present as totals for compat)

Author: QuantumAlpha
Version: 7.2.0
"""

from __future__ import annotations

import logging
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

logger = logging.getLogger("qa.daily_digest")


# ---------------------------------------------------------------------------
# Common SQLite helpers (read-only, unchanged from Phase 7.1)
# ---------------------------------------------------------------------------

@contextmanager
def _readonly_conn(db_path: Path) -> Iterator[Optional[sqlite3.Connection]]:
    if not db_path.exists():
        logger.warning("DB not found, skipping: %s", db_path)
        yield None
        return
    conn: Optional[sqlite3.Connection] = None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.DatabaseError:
            pass
        yield conn
    except sqlite3.DatabaseError as e:
        logger.warning("DB open failed (%s): %s", db_path, e)
        yield None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Log windowing — Issue 4 fix
# ---------------------------------------------------------------------------

# Strict timestamp parser. Accepts "YYYY-MM-DD HH:MM:SS" or ISO with T separator.
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def _parse_log_timestamp(line: str) -> Optional[datetime]:
    """Extract leading timestamp from log line. Returns UTC datetime or None."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.fromisoformat(m.group(1).replace(" ", "T"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None


def _iterate_log_window(
    logs_path: Path,
    cutoff: datetime,
    max_bytes: int = 50 * 1024 * 1024,
) -> Iterator[str]:
    """Yield log lines whose (inherited) timestamp >= cutoff.

    Handles:
      - Multi-line entries: lines without timestamps inherit from the most
        recent timestamped line above. Skipped if that line is pre-cutoff.
      - Huge files: seeks to last `max_bytes`, discards partial first line.
      - Pre-any-timestamp lines: included optimistically (best effort).

    Issue 4 root cause: previous implementation counted lines without
    timestamps regardless of context. This broke stacktrace handling.
    """
    if not logs_path.exists():
        return

    try:
        file_size = logs_path.stat().st_size
    except OSError as e:
        logger.warning("Cannot stat %s: %s", logs_path, e)
        return

    seek_from = max(0, file_size - max_bytes)

    try:
        with logs_path.open("r", encoding="utf-8", errors="replace") as f:
            if seek_from > 0:
                f.seek(seek_from)
                f.readline()  # discard partial first line after seek

            last_ts: Optional[datetime] = None
            for line in f:
                ts = _parse_log_timestamp(line)
                if ts is not None:
                    last_ts = ts

                # Skip if we have an anchor AND it's out of window
                if last_ts is not None and last_ts < cutoff:
                    continue

                # Otherwise yield (either in-window OR no anchor yet)
                yield line
    except OSError as e:
        logger.warning("Log read failed (%s): %s", logs_path, e)
        return


# ---------------------------------------------------------------------------
# Log event aggregation — Issues 1 + 4
# ---------------------------------------------------------------------------

# Strict marker introduced in Phase 7.2 across all strategy modules.
# Format: [EVAL_TICK] strategy=<id> [other key=value pairs]
_EVAL_TICK_RE = re.compile(r"\[EVAL_TICK\]\s+strategy=(\S+)")

# Severity-prefixed log levels (standard Python logging format)
_SEVERITY_RES = {
    "CRITICAL": re.compile(r"\[CRITICAL\]|\bCRITICAL\b"),
    "ERROR":    re.compile(r"\[ERROR\]|\bERROR\b"),
    "WARNING":  re.compile(r"\[WARNING\]|\bWARNING\b"),
}

# Non-severity patterns (event-style, kept from Phase 7.1)
_OTHER_PATTERNS: Dict[str, re.Pattern] = {
    "trades_opened":      re.compile(r"\b(trade opened|order filled|position opened|opened position)\b", re.I),
    "trades_closed":      re.compile(r"\b(trade closed|position closed|closed position|exit filled)\b", re.I),
    "scheduler_runs":     re.compile(r"\b(scheduler|cron)\b.*\b(fired|completed)\b", re.I),
    "telegram_reconnects": re.compile(r"\bServerDisconnectedError\b|\b(telegram).*reconnect", re.I),
    "dxy_missing":        re.compile(r"\bDX-Y\.NYB\b|\bDXY\b.*\b(missing|unavailable|fail)", re.I),
    "vix_missing":        re.compile(r"\b\^?VIX\b.*\b(missing|unavailable|fail)", re.I),
}


def aggregate_log_events(
    logs_path: Path,
    window_hours: int = 24,
    now: Optional[datetime] = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> Dict[str, object]:
    """Aggregate log events within last `window_hours`.

    Returns a dict with:
      - severity_counts: {"CRITICAL": N, "ERROR": N, "WARNING": N}
      - errors: total ERROR + CRITICAL (legacy compat key)
      - warnings: WARNING count (legacy compat key)
      - eval_ticks_by_strategy: {"liquidity_vortex_v1": N, "funding_arb_v1": M, ...}
        — counts of [EVAL_TICK] strategy=<name> markers
      - trades_opened, trades_closed, scheduler_runs, telegram_reconnects
      - dxy_missing, vix_missing
      - total_lines_scanned

    Missing log file → all zeros / empty dicts.
    """
    counts: Dict[str, object] = {
        "severity_counts": {"CRITICAL": 0, "ERROR": 0, "WARNING": 0},
        "errors": 0,
        "warnings": 0,
        "eval_ticks_by_strategy": {},
        "total_lines_scanned": 0,
    }
    for key in _OTHER_PATTERNS:
        counts[key] = 0

    cutoff = (now or _utc_now()) - timedelta(hours=window_hours)
    eval_ticks: Dict[str, int] = {}
    severities: Dict[str, int] = {"CRITICAL": 0, "ERROR": 0, "WARNING": 0}
    scanned = 0

    for line in _iterate_log_window(logs_path, cutoff, max_bytes=max_bytes):
        scanned += 1

        # Severity (check CRITICAL before ERROR to avoid double-count via ordering;
        # but we want both flagged because "CRITICAL ... ERROR" lines are rare).
        # Each line counts for AT MOST one severity bucket (mutually exclusive).
        if _SEVERITY_RES["CRITICAL"].search(line):
            severities["CRITICAL"] += 1
        elif _SEVERITY_RES["ERROR"].search(line):
            severities["ERROR"] += 1
        elif _SEVERITY_RES["WARNING"].search(line):
            severities["WARNING"] += 1

        # EVAL_TICK markers — strict
        m = _EVAL_TICK_RE.search(line)
        if m:
            sname = m.group(1)
            eval_ticks[sname] = eval_ticks.get(sname, 0) + 1

        # Other event patterns
        for name, pattern in _OTHER_PATTERNS.items():
            if pattern.search(line):
                counts[name] = counts[name] + 1  # type: ignore[operator]

    counts["severity_counts"] = severities
    counts["errors"] = severities["CRITICAL"] + severities["ERROR"]
    counts["warnings"] = severities["WARNING"]
    counts["eval_ticks_by_strategy"] = eval_ticks
    counts["total_lines_scanned"] = scanned

    return counts


# ---------------------------------------------------------------------------
# Equity DB aggregation — unchanged from Phase 7.1
# ---------------------------------------------------------------------------

_EQUITY_TABLE_CANDIDATES = (
    "equity_snapshots", "equity_history", "snapshots", "equity",
)
_EQUITY_COL_VALUE = ("equity", "value", "balance", "total")
_EQUITY_COL_TIME = ("snapshot_utc", "timestamp_utc", "fetched_utc", "ts", "timestamp")


def aggregate_equity_changes(
    db_path: Path,
    window_hours: int = 24,
    now: Optional[datetime] = None,
) -> Dict[str, Optional[float]]:
    """Return {start, end, delta, delta_pct, max_dd_pct, snapshot_count}."""
    empty: Dict[str, Optional[float]] = {
        "start": None, "end": None, "delta": None, "delta_pct": None,
        "max_dd_pct": None, "snapshot_count": 0.0,
    }
    cutoff = (now or _utc_now()) - timedelta(hours=window_hours)
    cutoff_iso = cutoff.isoformat()

    with _readonly_conn(db_path) as conn:
        if conn is None:
            return empty

        table: Optional[str] = None
        value_col: Optional[str] = None
        time_col: Optional[str] = None
        for t in _EQUITY_TABLE_CANDIDATES:
            if not _table_exists(conn, t):
                continue
            try:
                cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({t})")}
            except sqlite3.DatabaseError:
                continue
            v = next((c for c in _EQUITY_COL_VALUE if c in cols), None)
            ts = next((c for c in _EQUITY_COL_TIME if c in cols), None)
            if v and ts:
                table, value_col, time_col = t, v, ts
                break

        if not table:
            return empty

        try:
            rows = conn.execute(
                f"SELECT {value_col} AS v, {time_col} AS t "
                f"FROM {table} WHERE {time_col} >= ? ORDER BY {time_col} ASC",
                (cutoff_iso,),
            ).fetchall()
        except sqlite3.DatabaseError as e:
            logger.warning("Equity query failed: %s", e)
            return empty

    if not rows:
        return empty

    values: List[float] = [float(r["v"]) for r in rows if r["v"] is not None]
    if not values:
        return empty

    start_val: float = values[0]
    end_val: float = values[-1]
    delta_val: float = end_val - start_val
    delta_pct_val: float = (delta_val / start_val * 100.0) if start_val > 0 else 0.0

    peak: float = values[0]
    max_dd_pct: float = 0.0
    for snap_val in values:
        if snap_val > peak:
            peak = snap_val
        if peak > 0:
            dd = (peak - snap_val) / peak * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd

    return {
        "start": round(start_val, 2),
        "end": round(end_val, 2),
        "delta": round(delta_val, 2),
        "delta_pct": round(delta_pct_val, 4),
        "max_dd_pct": round(max_dd_pct, 4),
        "snapshot_count": float(len(values)),
    }


# ---------------------------------------------------------------------------
# Funding rates DB aggregation — unchanged
# ---------------------------------------------------------------------------

_FUNDING_TABLE_CANDIDATES = ("funding_rate_history", "funding_rates", "funding")


def aggregate_funding_rates(
    db_path: Path,
    window_hours: int = 24,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, float]]:
    result: Dict[str, Dict[str, float]] = {}
    cutoff = (now or _utc_now()) - timedelta(hours=window_hours)
    cutoff_iso = cutoff.isoformat()

    with _readonly_conn(db_path) as conn:
        if conn is None:
            return result
        table = next((t for t in _FUNDING_TABLE_CANDIDATES if _table_exists(conn, t)), None)
        if not table:
            return result
        try:
            rows = conn.execute(
                f"SELECT symbol, funding_rate, fetched_utc FROM {table} "
                f"WHERE fetched_utc >= ? ORDER BY fetched_utc ASC",
                (cutoff_iso,),
            ).fetchall()
        except sqlite3.DatabaseError as e:
            logger.warning("Funding query failed: %s", e)
            return result

    by_symbol: Dict[str, List[float]] = {}
    for r in rows:
        sym = r["symbol"]
        try:
            rate = float(r["funding_rate"])
        except (TypeError, ValueError):
            continue
        by_symbol.setdefault(sym, []).append(rate)

    for sym, rates in by_symbol.items():
        if not rates:
            continue
        result[sym] = {
            "current": round(rates[-1], 6),
            "median_24h": round(statistics.median(rates), 6),
            "count": len(rates),
        }
    return result


# ---------------------------------------------------------------------------
# Provider helpers — unchanged
# ---------------------------------------------------------------------------

def gather_calendar_today(
    calendar_provider: Optional[Callable[[], List[dict]]],
    window_hours: int = 24,
    now: Optional[datetime] = None,
) -> List[dict]:
    if calendar_provider is None:
        return []
    try:
        events = calendar_provider() or []
    except Exception as e:
        logger.warning("calendar_provider raised: %s", e)
        return []

    anchor = now or _utc_now()
    cutoff = anchor + timedelta(hours=window_hours)
    out: List[dict] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = ev.get("time_utc")
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(t, datetime):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if anchor <= t <= cutoff:
            out.append({
                "time_utc": t.isoformat(),
                "name": str(ev.get("name", "Unknown")),
                "importance": str(ev.get("importance", "medium")),
            })

    out.sort(key=lambda x: x["time_utc"])
    return out


def gather_bot_health(
    state_provider: Optional[Callable[[], dict]],
) -> Dict[str, object]:
    default: Dict[str, object] = {
        "uptime_sec": None,
        "memory_mb": None,
        "restart_count": None,
        "provider_healthy": None,
        "open_positions": None,
        "strategies": {},
    }
    if state_provider is None:
        return default
    try:
        raw = state_provider() or {}
    except Exception as e:
        logger.warning("state_provider raised: %s", e)
        return default

    if not isinstance(raw, dict):
        return default

    result: Dict[str, object] = {
        "uptime_sec": raw.get("uptime_sec"),
        "memory_mb": raw.get("memory_mb"),
        "restart_count": raw.get("restart_count"),
        "provider_healthy": raw.get("provider_healthy"),
        "open_positions": raw.get("open_positions"),
        "strategies": raw.get("strategies") or {},
    }
    return result


def gather_trade_trigger_status() -> Dict[str, object]:
    import subprocess
    info: Dict[str, object] = {"available": False, "active": False, "error": None}
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "qa-trade-trigger.service"],
            capture_output=True, text=True, timeout=3.0,
        )
        info["available"] = True
        status = result.stdout.strip()
        info["active"] = (status == "active")
        info["status"] = status
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info
