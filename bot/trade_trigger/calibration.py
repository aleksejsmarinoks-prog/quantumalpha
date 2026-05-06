"""
QA Trade Trigger — Calibration Engine
======================================

Analyzes historical signal performance to suggest threshold adjustments.

Inputs (from DB):
  - triggered_signals: actionability scores, user actions, realized outcomes
  - filter_audit_log: which gates rejected what

Outputs:
  - confirmation_rate: % of signals user confirmed
  - skip_rate: % of signals user skipped
  - rejection_distribution: which gate caused most rejections
  - suggested TT_MIN_SCORE: based on confirmed-signal score distribution
  - per-source health summary

Used by /tt_calibrate command.

Author: QuantumAlpha
Version: 0.4.0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from statistics import median
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .db import TradeTriggerDB


@dataclass
class CalibrationReport:
    period_days: int
    total_signals: int
    confirmed: int
    skipped: int
    no_action: int

    confirmation_rate: float           # confirmed / (confirmed+skipped)
    avg_score_confirmed: Optional[float]
    avg_score_skipped: Optional[float]
    median_score_all: Optional[float]

    rejection_counts: Dict[str, int]   # filter_name → reject count
    top_rejection_reasons: List[str]   # top 5 most common reject reasons

    source_event_counts: Dict[str, int]  # source_domain → events
    source_signal_counts: Dict[str, int]  # source_domain → signals fired

    suggested_min_score: Optional[float]
    suggested_min_score_rationale: str

    def to_text(self) -> str:
        lines = [
            f"CALIBRATION REPORT — {self.period_days} day window",
            "─" * 40,
            f"Signals fired: {self.total_signals}",
            f"  confirmed:  {self.confirmed}",
            f"  skipped:    {self.skipped}",
            f"  no action:  {self.no_action}",
            "",
        ]
        if self.confirmed + self.skipped > 0:
            lines.append(
                f"Confirmation rate: {self.confirmation_rate*100:.1f}% "
                f"({self.confirmed}/{self.confirmed + self.skipped})"
            )
        if self.avg_score_confirmed is not None:
            lines.append(f"Avg score confirmed: {self.avg_score_confirmed:.2f}")
        if self.avg_score_skipped is not None:
            lines.append(f"Avg score skipped:   {self.avg_score_skipped:.2f}")
        if self.median_score_all is not None:
            lines.append(f"Median score (all):  {self.median_score_all:.2f}")
        lines.append("")

        if self.rejection_counts:
            lines.append("Rejections by gate:")
            for name, cnt in sorted(
                self.rejection_counts.items(), key=lambda x: -x[1],
            )[:6]:
                lines.append(f"  {name:20s} {cnt}")
            lines.append("")

        if self.source_event_counts:
            lines.append("Events per source:")
            for src, cnt in sorted(
                self.source_event_counts.items(), key=lambda x: -x[1],
            )[:8]:
                signals = self.source_signal_counts.get(src, 0)
                lines.append(f"  {src:25s} events={cnt}  signals={signals}")
            lines.append("")

        if self.suggested_min_score is not None:
            lines.append(f"Suggested TT_MIN_SCORE: {self.suggested_min_score:.1f}")
            lines.append(f"  Rationale: {self.suggested_min_score_rationale}")
        else:
            lines.append("Suggested TT_MIN_SCORE: insufficient data (need ≥5 confirmations)")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def calibrate(db: "TradeTriggerDB", period_days: int = 7) -> CalibrationReport:
    """Run calibration analysis over the last `period_days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()

    with db._conn() as c:
        # Signals
        signal_rows = c.execute(
            """SELECT s.id, s.actionability_score, s.user_action, e.source_domain
               FROM triggered_signals s
               LEFT JOIN news_events e ON e.raw_id = s.raw_id
               WHERE s.fired_utc >= ?""",
            (cutoff,),
        ).fetchall()

        # Audit rejections (only failed steps)
        audit_rows = c.execute(
            """SELECT filter_name, reason
               FROM filter_audit_log
               WHERE passed = 0 AND checked_utc >= ?""",
            (cutoff,),
        ).fetchall()

        # All events per source
        source_event_rows = c.execute(
            """SELECT source_domain, COUNT(*) as cnt
               FROM news_events
               WHERE inserted_utc >= ?
               GROUP BY source_domain""",
            (cutoff,),
        ).fetchall()

    # ---- Signal stats ----
    total = len(signal_rows)
    confirmed_scores: List[float] = []
    skipped_scores: List[float] = []
    no_action = 0
    source_signal_counts: Dict[str, int] = {}

    for r in signal_rows:
        score = r["actionability_score"]
        action = r["user_action"]
        domain = r["source_domain"] or "unknown"
        source_signal_counts[domain] = source_signal_counts.get(domain, 0) + 1
        if action == "confirmed":
            confirmed_scores.append(score)
        elif action == "skipped":
            skipped_scores.append(score)
        else:
            no_action += 1

    confirmed = len(confirmed_scores)
    skipped = len(skipped_scores)
    confirmation_rate = (
        confirmed / (confirmed + skipped) if (confirmed + skipped) > 0 else 0.0
    )
    avg_conf = sum(confirmed_scores) / len(confirmed_scores) if confirmed_scores else None
    avg_skip = sum(skipped_scores) / len(skipped_scores) if skipped_scores else None
    all_scores = confirmed_scores + skipped_scores + [
        r["actionability_score"] for r in signal_rows if r["user_action"] is None
    ]
    med_score = median(all_scores) if all_scores else None

    # ---- Rejection stats ----
    rejection_counts: Dict[str, int] = {}
    top_reasons: List[str] = []
    for r in audit_rows:
        name = r["filter_name"] or "unknown"
        rejection_counts[name] = rejection_counts.get(name, 0) + 1
        reason = r["reason"]
        if reason and len(top_reasons) < 50:
            top_reasons.append(reason)

    # Most common reasons (truncated)
    from collections import Counter
    most_common_reasons = [
        f"{r[:80]} ({n}x)" for r, n in Counter(top_reasons).most_common(5)
    ]

    # ---- Source events ----
    source_event_counts = {r["source_domain"]: r["cnt"] for r in source_event_rows}

    # ---- Suggested min_score ----
    # Logic: if we have ≥5 confirmations, suggest score that captures ~80%
    # of confirmed signals (the lower 20% of confirmed scores becomes the floor).
    suggested = None
    rationale = "insufficient confirmations to suggest"
    if confirmed >= 5:
        sorted_conf = sorted(confirmed_scores)
        # 20th percentile of confirmed scores
        idx = max(0, int(0.2 * len(sorted_conf)))
        floor = sorted_conf[idx]
        # Round down to nearest 0.5
        suggested = round(floor * 2) / 2
        rationale = (
            f"20th percentile of {confirmed} confirmed signals is {floor:.2f}; "
            f"setting TT_MIN_SCORE={suggested} captures ~80% historical confirmations"
        )

    return CalibrationReport(
        period_days=period_days,
        total_signals=total,
        confirmed=confirmed,
        skipped=skipped,
        no_action=no_action,
        confirmation_rate=confirmation_rate,
        avg_score_confirmed=avg_conf,
        avg_score_skipped=avg_skip,
        median_score_all=med_score,
        rejection_counts=rejection_counts,
        top_rejection_reasons=most_common_reasons,
        source_event_counts=source_event_counts,
        source_signal_counts=source_signal_counts,
        suggested_min_score=suggested,
        suggested_min_score_rationale=rationale,
    )
