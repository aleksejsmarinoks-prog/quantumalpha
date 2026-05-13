"""
QA Backtester — Report Generation
==================================

Writes JSON and Markdown reports from walk-forward results.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import WindowResult
from .walk_forward import aggregate_verdict


log = logging.getLogger("qa.backtester.report")


# ─────────────────────────────────────────────────────────────────────────────
# JSON serialisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat()
    if hasattr(obj, "value") and hasattr(obj, "__class__") and obj.__class__.__name__.endswith("Enum"):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"Type {type(obj)} not JSON-serialisable")


def window_to_dict(w: WindowResult) -> dict:
    return {
        "window_idx": w.window_idx,
        "train_start": w.train_start.isoformat(),
        "train_end": w.train_end.isoformat(),
        "test_start": w.test_start.isoformat(),
        "test_end": w.test_end.isoformat(),
        "best_params": w.best_params,
        "train_metrics": w.train_metrics,
        "test_metrics": w.test_metrics,
        "train_trades": w.train_trades,
        "test_trades": w.test_trades,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Markdown formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_markdown(
    strategy_name: str,
    period_start: datetime,
    period_end: datetime,
    capital_usd: float,
    windows: list[WindowResult],
    verdict: dict,
) -> str:
    lines: list[str] = []
    lines.append(f"# Backtest: {strategy_name}")
    lines.append(f"Period: {period_start.date()} to {period_end.date()} "
                 f"({(period_end - period_start).days} days)")
    lines.append(f"Capital: ${capital_usd:,.0f}")
    lines.append("")
    lines.append("## Walk-Forward Summary")

    if not windows:
        lines.append("**No windows produced — insufficient data.**")
        return "\n".join(lines)

    lines.append("")
    lines.append("| Window | Train Sharpe | Test Sharpe | Test Win Rate | Test MDD | Test Trades | Test PnL$ |")
    lines.append("|--------|--------------|-------------|---------------|----------|-------------|-----------|")
    for w in windows:
        ts = w.train_metrics.get("sharpe_annualized", 0.0)
        tts = w.test_metrics.get("sharpe_annualized", 0.0)
        twr = w.test_metrics.get("win_rate", 0.0)
        tmdd = w.test_metrics.get("max_drawdown_pct", 0.0)
        ntr = w.test_trades
        tpnl = w.test_metrics.get("total_pnl_usd", 0.0)
        lines.append(f"| {w.window_idx + 1} | {ts:.2f} | {tts:.2f} | {twr:.2%} | {tmdd:.2%} | {ntr} | ${tpnl:+,.2f} |")

    lines.append("")
    lines.append("## Aggregate Verdict")
    lines.append("")
    lines.append(f"- **Median test Sharpe**: {verdict['median_test_sharpe']:.2f} "
                 f"({'PASS' if verdict['passes_sharpe'] else 'FAIL'} — gate ≥ 1.0)")
    lines.append(f"- **Max test MDD**: {verdict['max_test_mdd_pct']:.2%} "
                 f"({'PASS' if verdict['passes_mdd'] else 'FAIL'} — gate ≤ 8%)")
    lines.append(f"- **Min test win rate**: {verdict['min_test_winrate']:.2%} "
                 f"({'PASS' if verdict['passes_winrate'] else 'FAIL'} — gate ≥ 38%)")
    lines.append(f"- **% profitable windows**: {verdict['pct_profitable_windows']:.2%} "
                 f"({'PASS' if verdict['passes_profitable_pct'] else 'FAIL'} — gate ≥ 60%)")
    lines.append("")
    lines.append(f"### Verdict: **{verdict['verdict_text']}**")
    lines.append("")

    # Best-params summary
    lines.append("## Best Parameters by Window")
    lines.append("")
    for w in windows:
        params_str = ", ".join(f"{k}={v}" for k, v in w.best_params.items()) or "(no parameters)"
        lines.append(f"- Window {w.window_idx + 1}: {params_str}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Top-level writer
# ─────────────────────────────────────────────────────────────────────────────

def write_backtest_report(
    strategy_name: str,
    period_start: datetime,
    period_end: datetime,
    capital_usd: float,
    windows: list[WindowResult],
    output_dir: Path,
    timestamp: Optional[datetime] = None,
) -> tuple[Path, Path]:
    """
    Write JSON + Markdown. Returns (json_path, md_path).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
    safe_name = strategy_name.replace("/", "_")

    json_path = output_dir / f"{safe_name}_backtest_{ts_str}.json"
    md_path = output_dir / f"{safe_name}_backtest_{ts_str}.md"

    verdict = aggregate_verdict(strategy_name, windows)

    payload = {
        "strategy_name": strategy_name,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "capital_usd": capital_usd,
        "generated_at": timestamp.isoformat(),
        "windows": [window_to_dict(w) for w in windows],
        "verdict": verdict,
    }
    json_path.write_text(json.dumps(payload, default=_json_default, indent=2))
    md_path.write_text(format_markdown(strategy_name, period_start, period_end, capital_usd, windows, verdict))
    log.info("report written: %s + %s", json_path.name, md_path.name)
    return json_path, md_path
