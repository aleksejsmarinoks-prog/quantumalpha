"""Tests for bot.backtest.load_bars (Phase 6.3.1b-A)."""

from __future__ import annotations

import gzip
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import Bar
from bot.backtest.load_bars import load_bars_from_cache, split_by_gap


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


# ===========================================================================
# split_by_gap
# ===========================================================================

class TestSplitByGap:

    def test_empty_returns_empty(self):
        assert split_by_gap([]) == []

    def test_single_bar(self):
        bar = Bar(timestamp=T0, open=1, high=1, low=1, close=1, volume=1)
        segs = split_by_gap([bar])
        assert len(segs) == 1
        assert segs[0] == [bar]

    def test_contiguous_5m_bars_one_segment(self):
        bars = [Bar(timestamp=T0 + timedelta(minutes=5 * i),
                    open=1, high=1, low=1, close=1, volume=1) for i in range(10)]
        segs = split_by_gap(bars, max_gap_minutes=60)
        assert len(segs) == 1
        assert len(segs[0]) == 10

    def test_gap_splits_into_two_segments(self):
        bar_a = Bar(timestamp=T0, open=1, high=1, low=1, close=1, volume=1)
        bar_b = Bar(timestamp=T0 + timedelta(minutes=5),
                     open=1, high=1, low=1, close=1, volume=1)
        bar_c = Bar(timestamp=T0 + timedelta(hours=2),
                     open=1, high=1, low=1, close=1, volume=1)
        segs = split_by_gap([bar_a, bar_b, bar_c], max_gap_minutes=60)
        assert len(segs) == 2
        assert len(segs[0]) == 2
        assert len(segs[1]) == 1

    def test_long_gap_year_apart(self):
        """Apr 2024 + Nov 2025 — the actual Phase 6.3.1b scenario."""
        apr_bars = [Bar(timestamp=datetime(2024, 4, 1, tzinfo=UTC) + timedelta(minutes=5 * i),
                         open=1, high=1, low=1, close=1, volume=1) for i in range(10)]
        nov_bars = [Bar(timestamp=datetime(2025, 11, 1, tzinfo=UTC) + timedelta(minutes=5 * i),
                         open=1, high=1, low=1, close=1, volume=1) for i in range(10)]
        segs = split_by_gap(apr_bars + nov_bars, max_gap_minutes=60)
        assert len(segs) == 2

    def test_unsorted_input_handled(self):
        """split_by_gap sorts input."""
        bar_a = Bar(timestamp=T0, open=1, high=1, low=1, close=1, volume=1)
        bar_b = Bar(timestamp=T0 + timedelta(minutes=5),
                     open=1, high=1, low=1, close=1, volume=1)
        # Pass in reverse order
        segs = split_by_gap([bar_b, bar_a], max_gap_minutes=60)
        assert len(segs) == 1
        assert segs[0][0].timestamp == T0   # sorted output


# ===========================================================================
# load_bars_from_cache
# ===========================================================================

class TestLoadBarsFromCache:

    def _write_cache_csv(self, folder: Path, filename: str, rows: list):
        """Write gzipped CSV to mock cache."""
        folder.mkdir(parents=True, exist_ok=True)
        out_path = folder / filename
        lines = [",open,high,low,close,volume"]
        for ts, o, h, l, c, v in rows:
            lines.append(f"{ts.isoformat()},{o},{h},{l},{c},{v}")
        with gzip.open(out_path, "wt") as fh:
            fh.write("\n".join(lines) + "\n")
        return out_path

    def test_missing_folder_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="cache folder not found"):
            load_bars_from_cache("ETHUSDT", "5m", tmp_path)

    def test_no_matching_files_raises(self, tmp_path):
        folder = tmp_path / "ETHUSDT" / "5m"
        folder.mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="No cache files"):
            load_bars_from_cache("ETHUSDT", "5m", tmp_path)

    def test_load_single_file(self, tmp_path):
        folder = tmp_path / "ETHUSDT" / "5m"
        rows = [
            (T0,                                 2000, 2010, 1990, 2005, 100),
            (T0 + timedelta(minutes=5),          2005, 2015, 2000, 2010, 200),
            (T0 + timedelta(minutes=10),         2010, 2020, 2005, 2015, 300),
        ]
        self._write_cache_csv(folder, "klines_20260101_20260102.csv.gz", rows)
        bars = load_bars_from_cache("ETHUSDT", "5m", tmp_path)
        assert len(bars) == 3
        assert bars[0].open == 2000
        assert bars[0].close == 2005
        assert bars[0].timestamp == T0
        assert bars[2].close == 2015

    def test_merge_two_files_sorts_and_dedups(self, tmp_path):
        folder = tmp_path / "ETHUSDT" / "5m"
        # File 1: covers T0 + 0-15min
        rows1 = [
            (T0,                                 2000, 2010, 1990, 2005, 100),
            (T0 + timedelta(minutes=5),          2005, 2015, 2000, 2010, 200),
        ]
        # File 2: covers T0 + 10-25min (with overlap on T0+10min)
        rows2 = [
            (T0 + timedelta(minutes=10),         2010, 2020, 2005, 2015, 300),
            (T0 + timedelta(minutes=15),         2015, 2025, 2010, 2020, 400),
        ]
        self._write_cache_csv(folder, "klines_20260101_first.csv.gz", rows1)
        self._write_cache_csv(folder, "klines_20260101_second.csv.gz", rows2)

        bars = load_bars_from_cache("ETHUSDT", "5m", tmp_path)
        # 2 + 2 = 4 unique timestamps
        assert len(bars) == 4
        # Sorted
        for i in range(len(bars) - 1):
            assert bars[i].timestamp < bars[i + 1].timestamp

    def test_missing_column_raises(self, tmp_path):
        folder = tmp_path / "ETHUSDT" / "5m"
        folder.mkdir(parents=True)
        out_path = folder / "klines_bad.csv.gz"
        # Missing 'volume' column
        with gzip.open(out_path, "wt") as fh:
            fh.write(",open,high,low,close\n")
            fh.write(f"{T0.isoformat()},1,2,0,1\n")
        with pytest.raises(ValueError, match="missing columns"):
            load_bars_from_cache("ETHUSDT", "5m", tmp_path)

    def test_naive_timestamps_localized_to_utc(self, tmp_path):
        """CSV with naive timestamps should be localized to UTC."""
        folder = tmp_path / "ETHUSDT" / "5m"
        folder.mkdir(parents=True)
        out_path = folder / "klines_naive.csv.gz"
        # Write naive ISO timestamps
        with gzip.open(out_path, "wt") as fh:
            fh.write(",open,high,low,close,volume\n")
            fh.write("2026-01-01 00:00:00,2000,2010,1990,2005,100\n")
            fh.write("2026-01-01 00:05:00,2005,2015,2000,2010,200\n")
        bars = load_bars_from_cache("ETHUSDT", "5m", tmp_path)
        assert len(bars) == 2
        assert bars[0].timestamp.tzinfo is not None
        assert bars[0].timestamp == T0
