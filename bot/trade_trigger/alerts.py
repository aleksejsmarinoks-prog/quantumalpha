"""
QA Trade Trigger — Alert Formatting
=====================================

Renders TradeSignal → Telegram-friendly text + inline keyboard.

No HTML/Markdown — plain text. Reasoning: parse_mode crashes are a known
pain point in the existing codebase (see commit b3db013 in main bot tree).
Plain text is bulletproof and Cyrillic-safe.

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from .models import TradeSignal, AssetTrigger, Direction

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# Visual constants — keep simple, work in Telegram plain text
# ---------------------------------------------------------------------------

ALERT_HEADER = "🚨 TRADE TRIGGER"
DIVIDER = "─" * 32

# Direction → glyph
DIR_GLYPH = {
    Direction.LONG: "▲ LONG",
    Direction.SHORT: "▼ SHORT",
    Direction.SKIP: "● SKIP",
}


# ---------------------------------------------------------------------------
# Public format functions
# ---------------------------------------------------------------------------

def format_alert(signal: TradeSignal, signal_id: int) -> str:
    """Telegram-bound text. Plain text only, no markdown."""
    lines: List[str] = []
    lines.append(f"{ALERT_HEADER} #{signal_id}")
    lines.append(DIVIDER)
    lines.append(f"Event:   {signal.event_type}")
    lines.append(f"Score:   {signal.actionability_score:.1f} / 10")
    lines.append(f"Sources: {len(signal.sources)} corroborating")
    lines.append(f"First seen: {_fmt_time(signal.first_seen_utc)} UTC")
    lines.append("")

    for i, t in enumerate(signal.triggers, 1):
        glyph = DIR_GLYPH.get(t.direction, t.direction.value.upper())
        lines.append(f"{i}. {t.ticker} ({t.venue})  {glyph}")
        lines.append(
            f"   conviction={t.conviction:.2f}  "
            f"size={t.suggested_size_pct_bucket:.1f}% bucket  "
            f"hl={t.half_life_minutes}m"
        )
        if t.invalidation_reason:
            lines.append(f"   invalidation: {t.invalidation_reason}")
        lines.append("")

    if signal.reasoning:
        lines.append("Reasoning:")
        # Wrap reasoning at ~80 chars for Telegram readability
        for chunk in _wrap_text(signal.reasoning, width=80):
            lines.append(f"  {chunk}")

    return "\n".join(lines)


def format_audit(signal_row: dict, audit_rows: List[dict]) -> str:
    """Render full filter audit trail for /tt_audit <signal_id>."""
    lines: List[str] = []
    lines.append(f"AUDIT #{signal_row.get('id', '?')}")
    lines.append(DIVIDER)
    lines.append(f"Event:   {signal_row.get('event_type', '?')}")
    lines.append(f"Score:   {signal_row.get('actionability_score', 0):.1f} / 10")
    lines.append(f"Headline: {(signal_row.get('headline') or '')[:120]}")
    lines.append(f"Source:   {signal_row.get('source_domain', '?')}")
    lines.append(f"Fired:    {signal_row.get('fired_utc', '?')}")
    lines.append(f"User:     {signal_row.get('user_action') or 'no action'}")
    lines.append("")

    if not audit_rows:
        lines.append("No audit log rows. Possibly an old signal.")
        return "\n".join(lines)

    lines.append("Filter trail:")
    for r in audit_rows:
        passed = "✅" if r.get("passed") else "❌"
        name = r.get("filter_name", "?")
        reason = (r.get("reason") or "")[:90]
        lines.append(f"  {passed} {name}")
        if reason:
            lines.append(f"     {reason}")
    return "\n".join(lines)


def format_sources(rows: List[dict]) -> str:
    """Render source_health table for /tt_sources."""
    lines: List[str] = []
    lines.append("SOURCE HEALTH")
    lines.append(DIVIDER)
    if not rows:
        lines.append("No sources registered yet. Polling may not have started.")
        return "\n".join(lines)

    for r in rows:
        name = r.get("source_name", "?")
        fails = r.get("consecutive_fails", 0)
        total_polls = r.get("total_polls", 0)
        total_events = r.get("total_events", 0)
        last_ok = r.get("last_success_utc") or "never"
        last_err = r.get("last_error")

        status = "🟢 OK" if fails == 0 else f"🔴 fail x{fails}"
        lines.append(f"{name}  {status}")
        lines.append(f"  polls={total_polls}  events={total_events}")
        lines.append(f"  last_ok={_short_time(last_ok)}")
        if last_err and fails > 0:
            lines.append(f"  err: {last_err[:80]}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_recent(rows: List[dict]) -> str:
    """Render recent_signals list for /tt_recent."""
    lines: List[str] = []
    lines.append("RECENT SIGNALS")
    lines.append(DIVIDER)
    if not rows:
        lines.append("No signals fired yet.")
        return "\n".join(lines)

    for r in rows:
        sid = r.get("id", "?")
        ev = r.get("event_type", "?")
        score = r.get("actionability_score", 0)
        action = r.get("user_action") or "no action"
        fired = _short_time(r.get("fired_utc"))
        lines.append(f"#{sid}  [{ev}]  {score:.1f}/10  {action}  ({fired})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------

def build_alert_keyboard(signal_id: int) -> "InlineKeyboardMarkup":
    """Three buttons: Confirm / Skip / Audit. Lazy-imports aiogram types."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Confirm", callback_data=f"tt:confirm:{signal_id}"),
            InlineKeyboardButton(text="❌ Skip",    callback_data=f"tt:skip:{signal_id}"),
            InlineKeyboardButton(text="📊 Audit",   callback_data=f"tt:audit:{signal_id}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fmt_time(t) -> str:
    if isinstance(t, datetime):
        return t.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(t, str):
        return t.replace("T", " ").split(".")[0]
    return str(t)


def _short_time(t) -> str:
    """Compressed timestamp for tables: 'MM-DD HH:MM'."""
    if not t:
        return "—"
    s = str(t).replace("T", " ").split(".")[0]
    # If full ISO (YYYY-MM-DD HH:MM:SS), drop seconds and year
    if len(s) >= 16:
        return s[5:16]  # MM-DD HH:MM
    return s


def _wrap_text(text: str, width: int = 80) -> List[str]:
    """Naive word wrap. No external dependency."""
    words = text.split()
    lines: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for w in words:
        if cur_len + len(w) + 1 > width and cur:
            lines.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += len(w) + 1
    if cur:
        lines.append(" ".join(cur))
    return lines or [text]
