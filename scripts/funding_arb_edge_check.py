"""Funding-arb edge check: ETH-SOL spread analysis.

R&D script (not production). Validates whether the funding-spread
mean-reversion thesis underlying FundingArbStrategy has measurable edge
before committing 40% capital allocation.

Inputs
------
data/backtest_cache/{ETHUSDT,SOLUSDT}/funding/funding_*.csv.gz
  Columns: ts (UTC, 8h-aligned), funding_rate (decimal per 8h).

Outputs
-------
/tmp/funding_arb_edge_report.json
/tmp/funding_arb_edge_report.md

Verdict classification (production thresholds open=0.04%/8h, close=0.015%/8h)
  EDGE_PRESENT : trade_count >= 20/yr AND mean_pnl > 0 AND win_rate >= 55%
  MARGINAL     : trade_count >= 10/yr AND mean_pnl > 0
  NO_EDGE      : otherwise
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "backtest_cache"

ETH_FILES = [
    DATA_DIR / "ETHUSDT" / "funding" / "funding_20240401_20241101.csv.gz",
    DATA_DIR / "ETHUSDT" / "funding" / "funding_20251101_20260501.csv.gz",
]
SOL_FILES = [
    DATA_DIR / "SOLUSDT" / "funding" / "funding_20240401_20241101.csv.gz",
    DATA_DIR / "SOLUSDT" / "funding" / "funding_20251101_20260501.csv.gz",
]

# Production thresholds (FundingArbStrategy: open=0.04%/8h, close=0.015%/8h)
DEFAULT_OPEN_THR = 0.0004
DEFAULT_CLOSE_THR = 0.00015
# Bybit perpetuals: taker 0.055% per fill × 4 fills (open ETH, open SOL, close ETH, close SOL)
DEFAULT_FEE_PCT_ROUND_TRIP = 0.0  # back-compat; v2 run passes 0.22

SIGNAL_THRESHOLDS = [0.00005, 0.0001, 0.00015, 0.0002, 0.0003, 0.0004, 0.0005]
AUTOCORR_LAGS = [1, 3, 6, 9, 24, 72]  # in 8h periods

PERIODS_PER_DAY = 3
PERIODS_PER_YEAR = 365 * PERIODS_PER_DAY  # 1095


def load_funding(files: Iterable[Path], symbol: str) -> pd.DataFrame:
    frames = []
    for f in files:
        if not f.exists():
            raise FileNotFoundError(f"{symbol} funding file missing: {f}")
        with gzip.open(f, "rt") as fh:
            df = pd.read_csv(fh, parse_dates=["ts"])
        df["src"] = f.name
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    nan_count = int(out["funding_rate"].isna().sum())
    if nan_count:
        print(f"[{symbol}] WARN dropped {nan_count} NaN rows", file=sys.stderr)
        out = out.dropna(subset=["funding_rate"]).reset_index(drop=True)
    out = out.rename(columns={"funding_rate": f"f_{symbol.lower()}"})
    return out[["ts", f"f_{symbol.lower()}"]]


def assign_segment(ts: pd.Timestamp) -> str:
    # 2024-04 to 2024-11 vs 2025-11 to 2026-05
    if ts.year == 2024:
        return "2024_bull"
    return "2025_26_bear"


def autocorr_at_lag(series: pd.Series, lag: int) -> float:
    if lag <= 0 or lag >= len(series):
        return float("nan")
    x = series.values
    a = x[:-lag]
    b = x[lag:]
    if len(a) < 2:
        return float("nan")
    sa, sb = np.std(a), np.std(b)
    if sa == 0 or sb == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def distribution_stats(series: pd.Series) -> dict:
    s = series.dropna()
    return {
        "n": int(s.size),
        "mean_pct": float(s.mean() * 100),
        "std_pct": float(s.std() * 100),
        "min_pct": float(s.min() * 100),
        "max_pct": float(s.max() * 100),
        "p01_pct": float(s.quantile(0.01) * 100),
        "p05_pct": float(s.quantile(0.05) * 100),
        "p25_pct": float(s.quantile(0.25) * 100),
        "p50_pct": float(s.quantile(0.50) * 100),
        "p75_pct": float(s.quantile(0.75) * 100),
        "p95_pct": float(s.quantile(0.95) * 100),
        "p99_pct": float(s.quantile(0.99) * 100),
    }


def signal_counts(series: pd.Series) -> dict:
    n = len(series)
    out = {}
    for thr in SIGNAL_THRESHOLDS:
        hits = int((series.abs() >= thr).sum())
        out[f"|spread|>={thr*100:.4f}%"] = {
            "count": hits,
            "pct_of_periods": round(hits / n * 100, 3) if n else 0.0,
        }
    return out


def naive_backtest(
    spread: pd.Series,
    open_thr: float,
    close_thr: float,
    ts: pd.Series | None = None,
    fee_pct_round_trip: float = 0.0,
) -> dict:
    """Long-the-spread carry: when |spread| crosses open_thr at period t,
    enter with direction = sign(spread_t). Hold and accumulate
    direction * spread_k per period k until |spread_k| <= close_thr or end of data.

    fee_pct_round_trip is subtracted from each trade's gross PnL as a flat cost
    (e.g. Bybit taker 0.055% × 4 fills = 0.22). Verdict uses NET PnL.
    """
    if len(spread) == 0 or spread.isna().all():
        raise ValueError("spread series is empty / all-NaN — refuse to backtest")
    if float(spread.abs().sum()) == 0:
        raise ValueError("spread series is degenerate (all zeros) — refuse to backtest")

    vals = spread.values
    ts_vals = ts.values if ts is not None else None
    n = len(vals)
    trades = []
    i = 0
    while i < n:
        if abs(vals[i]) >= open_thr:
            direction = 1 if vals[i] > 0 else -1
            entry_idx = i
            gross_pnl = 0.0
            j = i
            while j < n:
                gross_pnl += direction * vals[j]
                if abs(vals[j]) <= close_thr and j > entry_idx:
                    break
                j += 1
            exit_idx = min(j, n - 1)
            hold_periods = exit_idx - entry_idx + 1
            gross_pnl_pct = float(gross_pnl * 100)
            net_pnl_pct = gross_pnl_pct - fee_pct_round_trip
            trade = {
                "entry_idx": int(entry_idx),
                "exit_idx": int(exit_idx),
                "hold_periods": int(hold_periods),
                "direction": int(direction),
                "entry_spread_pct": float(vals[entry_idx] * 100),
                "exit_spread_pct": float(vals[exit_idx] * 100),
                "gross_pnl_pct": round(gross_pnl_pct, 4),
                "fee_pct": round(fee_pct_round_trip, 4),
                "net_pnl_pct": round(net_pnl_pct, 4),
            }
            if ts_vals is not None:
                trade["entry_ts"] = str(pd.Timestamp(ts_vals[entry_idx]))
                trade["exit_ts"] = str(pd.Timestamp(ts_vals[exit_idx]))
            trades.append(trade)
            i = exit_idx + 1
        else:
            i += 1

    trade_count = len(trades)
    observed_periods = n
    base = {
        "open_thr_pct": open_thr * 100,
        "close_thr_pct": close_thr * 100,
        "fee_pct_round_trip": fee_pct_round_trip,
        "trade_count": trade_count,
        "trade_count_per_year": round(trade_count * PERIODS_PER_YEAR / observed_periods, 2),
        "trades": trades,
    }
    if trade_count == 0:
        base.update({
            "win_rate_pct": float("nan"),
            "mean_gross_pnl_pct": float("nan"),
            "mean_net_pnl_pct": float("nan"),
            "median_net_pnl_pct": float("nan"),
            "total_gross_pnl_pct": 0.0,
            "total_net_pnl_pct": 0.0,
            "annualized_net_pnl_pct": 0.0,
            "mean_hold_periods": float("nan"),
        })
        return base

    gross = np.array([t["gross_pnl_pct"] for t in trades])
    net = np.array([t["net_pnl_pct"] for t in trades])
    holds = np.array([t["hold_periods"] for t in trades])
    wins = int((net > 0).sum())
    total_net = float(net.sum())
    annualized_net = total_net * (PERIODS_PER_YEAR / observed_periods)
    base.update({
        "win_rate_pct": round(wins / trade_count * 100, 2),
        "mean_gross_pnl_pct": round(float(gross.mean()), 4),
        "mean_net_pnl_pct": round(float(net.mean()), 4),
        "median_net_pnl_pct": round(float(np.median(net)), 4),
        "total_gross_pnl_pct": round(float(gross.sum()), 4),
        "total_net_pnl_pct": round(total_net, 4),
        "annualized_net_pnl_pct": round(annualized_net, 3),
        "mean_hold_periods": round(float(holds.mean()), 2),
    })
    return base


def classify_verdict(bt: dict) -> str:
    tc = bt.get("trade_count_per_year", 0)
    mp = bt.get("mean_net_pnl_pct", float("nan"))
    wr = bt.get("win_rate_pct", float("nan"))
    if pd.isna(mp) or pd.isna(wr):
        return "NO_EDGE"
    if tc >= 20 and mp > 0 and wr >= 55:
        return "EDGE_PRESENT"
    if tc >= 10 and mp > 0:
        return "MARGINAL"
    return "NO_EDGE"


def analyze_segment(name: str, df: pd.DataFrame, open_thr: float, close_thr: float, fee_pct: float) -> dict:
    spread = df["spread"]
    autocorr = {f"lag_{k}_periods_{k*8}h": autocorr_at_lag(spread, k) for k in AUTOCORR_LAGS}
    dist = distribution_stats(spread)
    sig = signal_counts(spread)
    bt = naive_backtest(spread, open_thr, close_thr, ts=df["ts"], fee_pct_round_trip=fee_pct)
    bt["verdict"] = classify_verdict(bt)
    return {
        "segment": name,
        "ts_start": str(df["ts"].iloc[0]),
        "ts_end": str(df["ts"].iloc[-1]),
        "n_periods": int(len(df)),
        "distribution": dist,
        "autocorrelation": autocorr,
        "signal_counts": sig,
        "naive_backtest": bt,
    }


def build_markdown(report: dict) -> str:
    lines = []
    lines.append("# Funding-Arb Edge Check — ETH-SOL Spread")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append("")
    lines.append("**Thesis under test:** ETH-SOL funding-rate spread shows exploitable")
    lines.append("mean-reverting carry edge at the configured thresholds")
    lines.append(f"(open=±{report['open_thr_pct']:.4f}%/8h, close=±{report['close_thr_pct']:.4f}%/8h).")
    lines.append("")
    lines.append("**PnL model:** enter at |spread|≥open_thr with direction=sign(spread),")
    lines.append("accumulate direction*spread per 8h until |spread|≤close_thr.")
    fee = report.get("fee_pct_round_trip", 0.0)
    if fee > 0:
        lines.append(f"Round-trip fee modeled: **{fee}%** subtracted from each trade gross PnL.")
    else:
        lines.append("No fees/slippage modeled.")
    lines.append("")

    for seg in report["segments"]:
        name = seg["segment"]
        lines.append(f"## Segment: `{name}`")
        lines.append("")
        lines.append(f"- Range: `{seg['ts_start']}` → `{seg['ts_end']}`")
        lines.append(f"- Periods (8h): {seg['n_periods']}")
        lines.append("")
        d = seg["distribution"]
        lines.append("### Spread distribution (% per 8h)")
        lines.append("")
        lines.append("| stat | value |")
        lines.append("|---|---:|")
        lines.append(f"| mean | {d['mean_pct']:.5f} |")
        lines.append(f"| std  | {d['std_pct']:.5f} |")
        lines.append(f"| min  | {d['min_pct']:.5f} |")
        lines.append(f"| p01  | {d['p01_pct']:.5f} |")
        lines.append(f"| p05  | {d['p05_pct']:.5f} |")
        lines.append(f"| p25  | {d['p25_pct']:.5f} |")
        lines.append(f"| p50  | {d['p50_pct']:.5f} |")
        lines.append(f"| p75  | {d['p75_pct']:.5f} |")
        lines.append(f"| p95  | {d['p95_pct']:.5f} |")
        lines.append(f"| p99  | {d['p99_pct']:.5f} |")
        lines.append(f"| max  | {d['max_pct']:.5f} |")
        lines.append("")
        lines.append("### Autocorrelation of spread")
        lines.append("")
        lines.append("| lag | rho |")
        lines.append("|---|---:|")
        for k, v in seg["autocorrelation"].items():
            v_str = "nan" if (isinstance(v, float) and (np.isnan(v))) else f"{v:.4f}"
            lines.append(f"| {k} | {v_str} |")
        lines.append("")
        lines.append("### Signal counts (|spread| ≥ threshold)")
        lines.append("")
        lines.append("| threshold | hits | % of periods |")
        lines.append("|---|---:|---:|")
        for thr_label, payload in seg["signal_counts"].items():
            lines.append(f"| {thr_label} | {payload['count']} | {payload['pct_of_periods']}% |")
        lines.append("")
        bt = seg["naive_backtest"]
        lines.append("### Naive backtest @ configured thresholds")
        lines.append("")
        lines.append(f"- open_thr = ±{bt['open_thr_pct']:.4f}%, close_thr = ±{bt['close_thr_pct']:.4f}%, fee = {bt['fee_pct_round_trip']}%/round-trip")
        lines.append(f"- Trades: {bt['trade_count']}  ({bt['trade_count_per_year']}/yr)")
        wr_str = "n/a" if pd.isna(bt['win_rate_pct']) else f"{bt['win_rate_pct']}%"
        mg_str = "n/a" if pd.isna(bt['mean_gross_pnl_pct']) else f"{bt['mean_gross_pnl_pct']}%"
        mn_str = "n/a" if pd.isna(bt['mean_net_pnl_pct']) else f"{bt['mean_net_pnl_pct']}%"
        med_str = "n/a" if pd.isna(bt['median_net_pnl_pct']) else f"{bt['median_net_pnl_pct']}%"
        mh_str = "n/a" if pd.isna(bt['mean_hold_periods']) else f"{bt['mean_hold_periods']}"
        lines.append(f"- Win rate (net): {wr_str}")
        lines.append(f"- Mean PnL/trade gross: {mg_str}  net: {mn_str}")
        lines.append(f"- Median net PnL/trade: {med_str}")
        lines.append(f"- Mean hold (periods): {mh_str}")
        lines.append(f"- Total gross PnL: {bt['total_gross_pnl_pct']}%   net: {bt['total_net_pnl_pct']}%")
        lines.append(f"- Annualized net PnL: {bt['annualized_net_pnl_pct']}%")
        lines.append("")
        if bt["trades"]:
            lines.append("#### Trade-level detail")
            lines.append("")
            lines.append("| # | entry_ts | exit_ts | dir | hold | entry% | exit% | gross% | net% |")
            lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
            for k, t in enumerate(bt["trades"], 1):
                lines.append(
                    f"| {k} | {t.get('entry_ts','')} | {t.get('exit_ts','')} | "
                    f"{t['direction']:+d} | {t['hold_periods']} | "
                    f"{t['entry_spread_pct']:.4f} | {t['exit_spread_pct']:.4f} | "
                    f"{t['gross_pnl_pct']:.4f} | {t['net_pnl_pct']:.4f} |"
                )
            lines.append("")
        lines.append(f"**Verdict ({name}): `{bt['verdict']}`**")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"## OVERALL VERDICT: `{report['overall_verdict']}`")
    lines.append("")
    lines.append(f"_Driver: combined segment_ `{report['driver_segment']}`")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Funding-arb ETH-SOL spread edge check.")
    p.add_argument("--open-thr", type=float, default=DEFAULT_OPEN_THR,
                   help="Open threshold in decimal (default 0.0004 = 0.04%%/8h).")
    p.add_argument("--close-thr", type=float, default=DEFAULT_CLOSE_THR,
                   help="Close threshold in decimal (default 0.00015 = 0.015%%/8h).")
    p.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT_ROUND_TRIP,
                   help="Round-trip fee in percent subtracted per trade (e.g. 0.22 for 0.055%% × 4 fills).")
    p.add_argument("--out-suffix", type=str, default="",
                   help="Suffix appended to output filenames (e.g. '_v2').")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_out = Path(f"/tmp/funding_arb_edge_report{args.out_suffix}.json")
    md_out = Path(f"/tmp/funding_arb_edge_report{args.out_suffix}.md")

    eth = load_funding(ETH_FILES, "ETH")
    sol = load_funding(SOL_FILES, "SOL")
    joined = pd.merge(eth, sol, on="ts", how="inner").sort_values("ts").reset_index(drop=True)

    if joined.empty:
        print("FATAL: empty join — no overlapping timestamps between ETH and SOL.", file=sys.stderr)
        return 2

    joined["spread"] = joined["f_eth"] - joined["f_sol"]
    joined["segment"] = joined["ts"].map(assign_segment)

    seg_bull = joined[joined["segment"] == "2024_bull"].reset_index(drop=True)
    seg_bear = joined[joined["segment"] == "2025_26_bear"].reset_index(drop=True)

    seg_reports = []
    seg_reports.append(analyze_segment("2024_bull", seg_bull, args.open_thr, args.close_thr, args.fee_pct))
    seg_reports.append(analyze_segment("2025_26_bear", seg_bear, args.open_thr, args.close_thr, args.fee_pct))
    seg_reports.append(analyze_segment("combined", joined.reset_index(drop=True), args.open_thr, args.close_thr, args.fee_pct))

    combined_report = seg_reports[-1]
    overall_verdict = combined_report["naive_backtest"]["verdict"]

    report = {
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "open_thr_pct": args.open_thr * 100,
        "close_thr_pct": args.close_thr * 100,
        "fee_pct_round_trip": args.fee_pct,
        "files": {
            "eth": [str(p) for p in ETH_FILES],
            "sol": [str(p) for p in SOL_FILES],
        },
        "n_total_periods": int(len(joined)),
        "segments": seg_reports,
        "overall_verdict": overall_verdict,
        "driver_segment": "combined",
    }

    json_out.write_text(json.dumps(report, indent=2, default=str))
    md = build_markdown(report)
    md_out.write_text(md)

    print(f"JSON: {json_out}")
    print(f"MD:   {md_out}")
    print()
    print(f"FINAL VERDICT (combined): {overall_verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
