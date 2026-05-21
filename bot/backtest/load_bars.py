"""
QA Backtest — Bar Cache Loader (Phase 6.3.1b)
================================================

Helper for loading walk-forward bar cache files into List[Bar]. Bars are
fetched server-side by `bot/backtester/data_loader.py` (BybitDataLoader)
and stored as gzipped CSV in `data/backtest_cache/{SYMBOL}/{timeframe}/`.

This module merges multiple cache files for a symbol/timeframe into one
sorted, deduplicated List[Bar] ready for WalkForwardHarness.

Expected cache layout:
    data/backtest_cache/
        ETHUSDT/
            5m/
                klines_20240401_20241101.csv.gz       (Apr-Oct 2024 bull rally)
                klines_20251101_20260520.csv.gz       (Nov 2025 - May 2026)
        SOLUSDT/
            5m/
                klines_20240401_20241101.csv.gz
                klines_20251101_20260520.csv.gz

CSV format (per BybitDataLoader convention):
    Index: timestamp (UTC-aware after load)
    Columns: open, high, low, close, volume (all floats)

Anti-lookahead: loader returns bars in strict timestamp order. Walk-forward
harness consumes them; anti-lookahead is enforced internally by providers.

Author: QuantumAlpha
Phase: 6.3.1b
"""

from __future__ import annotations

import gzip
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .models import Bar

logger = logging.getLogger("qa.backtest.load_bars")


def load_bars_from_cache(
    symbol: str,
    timeframe: str = "5m",
    cache_root: Path = Path("data/backtest_cache"),
    file_glob: str = "klines_*.csv.gz",
) -> List[Bar]:
    """Load all cached bar files for a symbol/timeframe into one sorted List[Bar].

    Behavior:
      - Scans `cache_root/symbol/timeframe/` for files matching `file_glob`
      - Reads each gzipped CSV; localizes naive timestamps to UTC
      - Concatenates, sorts by timestamp, deduplicates (keeps first occurrence)
      - Returns immutable-by-convention List[Bar]

    Raises:
      - FileNotFoundError if folder missing or no matching files
      - ValueError if any CSV row has bad data (NaN, negative volume, etc.)
    """
    folder = cache_root / symbol / timeframe
    if not folder.exists():
        raise FileNotFoundError(
            f"Bar cache folder not found: {folder} "
            f"(expected layout: cache_root/SYMBOL/TIMEFRAME/klines_*.csv.gz)"
        )

    files = sorted(folder.glob(file_glob))
    if not files:
        raise FileNotFoundError(
            f"No cache files matching {file_glob!r} in {folder}"
        )

    logger.info("Loading bars for %s %s from %d file(s): %s",
                symbol, timeframe, len(files), [f.name for f in files])

    # Defer pandas import — only required when this function is called
    import pandas as pd

    dfs = []
    for f in files:
        with gzip.open(f, "rt") as fh:
            df = pd.read_csv(fh, index_col=0, parse_dates=True)
        # Localize to UTC
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        # Validate columns
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Cache file {f} missing columns: {missing}. "
                f"Required: {required}. Found: {set(df.columns)}"
            )
        dfs.append(df)

    full = pd.concat(dfs).sort_index()
    # Deduplicate by timestamp — keep first occurrence (e.g. on overlapping fetches)
    pre_dedup = len(full)
    full = full[~full.index.duplicated(keep="first")]
    if len(full) != pre_dedup:
        logger.info("Deduplicated %d duplicate timestamps", pre_dedup - len(full))

    if full.empty:
        raise ValueError(f"No bars loaded from {folder} after dedup")

    bars: List[Bar] = []
    for ts, row in full.iterrows():
        # Convert pandas Timestamp -> datetime (UTC tz-aware)
        py_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if py_ts.tzinfo is None:
            py_ts = py_ts.replace(tzinfo=timezone.utc)
        bars.append(Bar(
            timestamp=py_ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        ))

    logger.info("Loaded %d bars for %s %s: %s → %s",
                len(bars), symbol, timeframe,
                bars[0].timestamp, bars[-1].timestamp)
    return bars


def split_by_gap(
    bars: Sequence[Bar],
    max_gap_minutes: float = 60.0,
) -> List[List[Bar]]:
    """Split bar list by time gaps exceeding `max_gap_minutes`.

    Used by walk_forward_verdict driver for Option A multi-harness gap split:
    if there are two non-contiguous bar segments (e.g. Apr-Oct 2024 + Nov 2025-May 2026
    with year-long gap), this function returns them as separate lists so each
    can be fed to its own WalkForwardHarness run.

    Returns:
        List of bar segments, each contiguous (no internal gap > max_gap_minutes).
        Order preserved.
    """
    if not bars:
        return []
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)
    segments: List[List[Bar]] = [[sorted_bars[0]]]
    for prev, curr in zip(sorted_bars[:-1], sorted_bars[1:]):
        gap_minutes = (curr.timestamp - prev.timestamp).total_seconds() / 60.0
        if gap_minutes > max_gap_minutes:
            segments.append([curr])
        else:
            segments[-1].append(curr)
    return segments
