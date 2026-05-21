"""
QA Phase 6.3.1b — mean_reversion_v1 Walk-Forward Verdict Driver
================================================================

Runs walk-forward backtest for the production `MeanReversionStrategy` on
real Bybit ETH 5m + SOL 5m bars. Produces verdict report (Markdown + JSON).

Scope (Aleksejs decisions, 20 May 2026):
  - mean_reversion_v1 ONLY (DcaDips deferred to Phase 6.3.1d after FRED
    integration delivers real VIX + corroborated geopolitical feed)
  - Mixed-regime data: Apr-Oct 2024 (bull rally) + Nov 2025-May 2026 (recent)
  - Multi-harness gap split Option A — driver-level (not framework-level)
  - Verdict thresholds: EDGE_CONFIRMED / MARGINAL / NO_EDGE

Prerequisites:
  - Cache files present at:
      data/backtest_cache/ETHUSDT/5m/klines_*.csv.gz
      data/backtest_cache/SOLUSDT/5m/klines_*.csv.gz
  - bot/backtest/adapters/real_mean_reversion_adapter.py exists with
    production-source-aligned `RealMeanReversionAdapter` (Step 6.3.1b-B)

Usage:
    python -m scripts.walk_forward_verdict
    # or
    python scripts/walk_forward_verdict.py

Outputs:
    /tmp/verdict_mean_reversion_ETHUSDT.json
    /tmp/verdict_mean_reversion_SOLUSDT.json
    /tmp/verdict_mean_reversion.md

Author: QuantumAlpha
Phase: 6.3.1b-A
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bot.backtest import (
    IndicatorsProvider,
    WalkForwardHarness, WalkForwardConfig, WalkForwardReport,
)
from bot.backtest.regime_detector import make_trend_regime_provider
from bot.backtest.load_bars import load_bars_from_cache, split_by_gap


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("verdict")


# ===========================================================================
# Configuration
# ===========================================================================

SYMBOLS = ["ETHUSDT", "SOLUSDT"]
INITIAL_EQUITY = 200.0   # mean_reversion = 20% of $1000 portfolio per Orchestra config
CACHE_ROOT = Path("data/backtest_cache")
OUTPUT_DIR = Path("/tmp")
MAX_GAP_MINUTES = 60.0   # any gap > 1h treated as data discontinuity

# Edge classification thresholds (Aleksejs approved 20 May 2026)
EDGE_CONFIRMED_MEAN_RETURN_PCT = 0.20
EDGE_CONFIRMED_WIN_PCT = 55.0
MARGINAL_WIN_PCT = 50.0


# ===========================================================================
# Per-symbol run
# ===========================================================================

def run_symbol(symbol: str, cache_root: Path = CACHE_ROOT) -> Dict:
    """Run walk-forward verdict for one symbol.

    Implements Option A multi-harness gap split:
      - Load all bars for symbol
      - Split into contiguous segments at gaps > MAX_GAP_MINUTES
      - Run WalkForwardHarness on each segment separately
      - Aggregate per-segment results manually

    Returns aggregate dict with merged stats across all segments.
    """
    log.info("=" * 70)
    log.info("Symbol: %s", symbol)
    log.info("=" * 70)

    # Late import — adapter may not exist in Step 6.3.1b-A yet (delivered in 6.3.1b-B)
    try:
        from bot.backtest.adapters.real_mean_reversion_adapter import (
            RealMeanReversionAdapter,
        )
    except ImportError as e:
        log.error(
            "RealMeanReversionAdapter not available — Step 6.3.1b-B not yet deployed: %s",
            e,
        )
        log.error("This driver needs `bot/backtest/adapters/real_mean_reversion_adapter.py`")
        raise SystemExit(2)

    bars = load_bars_from_cache(symbol, "5m", cache_root)
    segments = split_by_gap(bars, max_gap_minutes=MAX_GAP_MINUTES)
    log.info("[%s] %d bars across %d contiguous segment(s)",
             symbol, len(bars), len(segments))

    all_window_summaries: List[Dict] = []
    per_segment_aggregates: List[Dict] = []
    total_succeeded = 0
    total_failed = 0
    total_windows = 0
    total_trades = 0
    all_returns: List[float] = []
    all_drawdowns: List[float] = []

    for seg_idx, segment in enumerate(segments):
        if len(segment) < 100:
            log.warning("[%s] segment %d too short (%d bars), skipping",
                         symbol, seg_idx, len(segment))
            continue

        seg_start = segment[0].timestamp
        seg_end = segment[-1].timestamp
        log.info("[%s] Segment %d: %d bars  %s → %s",
                 symbol, seg_idx, len(segment), seg_start, seg_end)

        harness = WalkForwardHarness(
            bars=segment,
            config=WalkForwardConfig(),    # default 30/7/7 rolling
            adapter_factory=lambda: RealMeanReversionAdapter(
                starting_capital_usd=INITIAL_EQUITY,
            ),
            indicators_provider_factory=lambda b: IndicatorsProvider(b).callable_for_engine(),
            regime_provider_factory=lambda b: make_trend_regime_provider(b),
            symbol=symbol,
            initial_equity=INITIAL_EQUITY,
        )
        report = harness.run()

        seg_agg = report.aggregate
        per_segment_aggregates.append({
            "segment_index": seg_idx,
            "segment_start": seg_start.isoformat(),
            "segment_end": seg_end.isoformat(),
            "segment_bars": len(segment),
            **seg_agg,
        })

        total_succeeded += seg_agg.get("windows_succeeded", 0)
        total_failed += seg_agg.get("windows_failed", 0)
        total_windows += seg_agg.get("windows_total", 0)
        total_trades += seg_agg.get("trades_total", 0)

        for w in report.successful_windows:
            if w.result is not None:
                all_returns.append(w.result.total_return_pct)
                all_drawdowns.append(w.result.max_drawdown_pct)
            all_window_summaries.append({"segment_index": seg_idx, **w.summary()})

        log.info("[%s] Segment %d done: %d windows OK, %d failed",
                 symbol, seg_idx,
                 seg_agg.get("windows_succeeded", 0),
                 seg_agg.get("windows_failed", 0))

    # Manual aggregation across all segments
    if all_returns:
        import statistics
        profitable = sum(1 for r in all_returns if r > 0)
        unprofitable = sum(1 for r in all_returns if r < 0)
        merged_aggregate = {
            "windows_total": total_windows,
            "windows_succeeded": total_succeeded,
            "windows_failed": total_failed,
            "windows_profitable": profitable,
            "windows_unprofitable": unprofitable,
            "windows_neutral": len(all_returns) - profitable - unprofitable,
            "trades_total": total_trades,
            "return_pct_mean": round(statistics.mean(all_returns), 4),
            "return_pct_median": round(statistics.median(all_returns), 4),
            "return_pct_min": round(min(all_returns), 4),
            "return_pct_max": round(max(all_returns), 4),
            "max_drawdown_pct_worst": round(max(all_drawdowns), 4),
            "max_drawdown_pct_mean": round(statistics.mean(all_drawdowns), 4),
        }
        if len(all_returns) > 1:
            merged_aggregate["return_pct_stdev"] = round(statistics.stdev(all_returns), 4)
    else:
        merged_aggregate = {
            "windows_total": total_windows,
            "windows_succeeded": 0,
            "windows_failed": total_failed,
            "trades_total": 0,
        }

    # Build verdict
    merged_aggregate["verdict"] = classify_edge(merged_aggregate)

    out_data = {
        "symbol": symbol,
        "ran_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_bars": len(bars),
        "segments_count": len(segments),
        "config": {
            "train_window_days": 30,
            "test_window_days": 7,
            "step_days": 7,
            "walk_type": "rolling",
            "multi_harness_gap_split": "Option A (driver-level)",
            "max_gap_minutes": MAX_GAP_MINUTES,
        },
        "per_segment": per_segment_aggregates,
        "aggregate": merged_aggregate,
        "windows": all_window_summaries,
    }

    out_path = OUTPUT_DIR / f"verdict_mean_reversion_{symbol}.json"
    out_path.write_text(json.dumps(out_data, indent=2, default=str))
    log.info("[%s] JSON: %s", symbol, out_path)
    log.info("[%s] Aggregate verdict: %s", symbol, merged_aggregate.get("verdict"))
    return merged_aggregate


# ===========================================================================
# Edge classification
# ===========================================================================

def classify_edge(agg: Dict) -> str:
    """Apply Aleksejs's 3-tier verdict thresholds.

    EDGE_CONFIRMED: mean per-window return > +0.2% AND win-window rate ≥ 55%
    MARGINAL:       mean > 0 AND win-window rate ≥ 50%
    NO_EDGE:        otherwise
    """
    succeeded = agg.get("windows_succeeded", 0)
    if succeeded == 0:
        return "NO_DATA"

    mean_ret = agg.get("return_pct_mean", 0.0) or 0.0
    profitable = agg.get("windows_profitable", 0)
    win_pct = (profitable / succeeded) * 100.0

    if mean_ret > EDGE_CONFIRMED_MEAN_RETURN_PCT and win_pct >= EDGE_CONFIRMED_WIN_PCT:
        return f"EDGE_CONFIRMED (mean {mean_ret:+.2f}%/window, win {win_pct:.0f}%)"
    if mean_ret > 0 and win_pct >= MARGINAL_WIN_PCT:
        return f"MARGINAL (mean {mean_ret:+.2f}%/window, win {win_pct:.0f}%)"
    return f"NO_EDGE (mean {mean_ret:+.2f}%/window, win {win_pct:.0f}%)"


# ===========================================================================
# Markdown report
# ===========================================================================

def build_markdown_verdict(results: Dict[str, Dict]) -> str:
    lines = [
        "# QA Phase 6.3.1b — Mean Reversion Walk-Forward Verdict",
        "",
        f"**Run UTC:** {datetime.now(timezone.utc).isoformat()}",
        "**Strategy:** mean_reversion_v1 (production source, RealMeanReversionAdapter)",
        "**Config:** rolling 30d train / 7d test / 7d step",
        f"**Capital per symbol:** ${INITIAL_EQUITY:.0f} (20% of $1000 portfolio)",
        "**Gap handling:** Option A multi-harness (driver-level split, "
        f"contiguous segments only, max_gap={MAX_GAP_MINUTES}min)",
        "",
        "## Per-symbol aggregate",
        "",
        "| Symbol | Windows OK | Profitable | Mean Ret % | Median Ret % | Stdev % | Worst DD % | Trades | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for sym, agg in results.items():
        win_ok = f"{agg.get('windows_succeeded', 0)}/{agg.get('windows_total', 0)}"
        profitable = agg.get("windows_profitable", 0)
        mean_ret = agg.get("return_pct_mean", "—")
        median_ret = agg.get("return_pct_median", "—")
        stdev = agg.get("return_pct_stdev", "—")
        worst_dd = agg.get("max_drawdown_pct_worst", "—")
        trades = agg.get("trades_total", 0)
        verdict = agg.get("verdict", "NO_DATA")
        lines.append(
            f"| {sym} | {win_ok} | {profitable} | {mean_ret} | {median_ret} | "
            f"{stdev} | {worst_dd} | {trades} | {verdict} |"
        )

    lines.extend(["", "## Verdict logic", ""])
    lines.extend([
        f"- **EDGE_CONFIRMED:** mean per-window return > +{EDGE_CONFIRMED_MEAN_RETURN_PCT}% "
        f"AND win-window rate ≥ {EDGE_CONFIRMED_WIN_PCT}%",
        f"- **MARGINAL:** mean > 0 AND win-window rate ≥ {MARGINAL_WIN_PCT}%",
        "- **NO_EDGE:** otherwise",
    ])

    lines.extend(["", "## Per-symbol verdict", ""])
    for sym, agg in results.items():
        lines.append(f"- **{sym}:** {agg.get('verdict', 'NO_DATA')}")

    lines.extend([
        "",
        "## Sanity check",
        "",
        f"**Per-window expected trade count:** ≥5 per symbol per 7 days in normal vol.",
        "If <1 trade on 50% of windows → suspect set_strategy_capital or regime gate still broken.",
        "",
        "## Raw data",
        "",
        "Full per-window JSON: `/tmp/verdict_mean_reversion_{symbol}.json`",
    ])

    return "\n".join(lines)


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    results: Dict[str, Dict] = {}
    for symbol in SYMBOLS:
        try:
            results[symbol] = run_symbol(symbol)
        except FileNotFoundError as e:
            log.error("[%s] Cache missing: %s", symbol, e)
            results[symbol] = {
                "windows_total": 0,
                "windows_succeeded": 0,
                "windows_failed": 0,
                "trades_total": 0,
                "verdict": "NO_DATA",
                "error": str(e),
            }
        except Exception as e:
            log.exception("[%s] Run failed", symbol)
            results[symbol] = {
                "windows_total": 0,
                "windows_succeeded": 0,
                "verdict": "ERROR",
                "error": f"{type(e).__name__}: {e}",
            }

    md = build_markdown_verdict(results)
    md_path = OUTPUT_DIR / "verdict_mean_reversion.md"
    md_path.write_text(md)
    log.info("Markdown: %s", md_path)

    print("\n" + "=" * 70)
    print(md)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
