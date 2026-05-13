"""Tests for bot.backtester.data_loader (with HTTP mock)."""
from __future__ import annotations

import gzip
from datetime import datetime, timezone

import pandas as pd
import pytest


class TestKlineFetch:
    def test_fetch_klines_basic(self, loader, fake_http):
        rows = [
            [1735689600000, "3500.0", "3510.0", "3490.0", "3505.0", "100.0", "350000.0"],
            [1735689900000, "3505.0", "3515.0", "3495.0", "3510.0", "120.0", "420000.0"],
        ]
        fake_http.queue_kline_chunk(rows)
        df = loader.fetch_klines(
            "ETHUSDT", "5m",
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 1, 1, tzinfo=timezone.utc),
        )
        assert not df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.tz is not None
        assert len(df) == 2

    def test_fetch_klines_caches_and_reuses(self, loader, fake_http, tmp_cache_dir):
        rows = [[1735689600000, "3500.0", "3510.0", "3490.0", "3505.0", "100.0", "350000.0"]]
        fake_http.queue_kline_chunk(rows)
        loader.fetch_klines(
            "ETHUSDT", "5m",
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 1, 1, tzinfo=timezone.utc),
        )
        calls_before = fake_http.call_count
        # Second fetch should hit cache → no new HTTP call
        df2 = loader.fetch_klines(
            "ETHUSDT", "5m",
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 1, 1, tzinfo=timezone.utc),
        )
        assert fake_http.call_count == calls_before
        assert len(df2) == 1

    def test_fetch_klines_force_refresh(self, loader, fake_http):
        rows = [[1735689600000, "3500.0", "3510.0", "3490.0", "3505.0", "100.0", "350000.0"]]
        fake_http.queue_kline_chunk(rows)
        loader.fetch_klines("ETHUSDT", "5m", datetime(2025, 1, 1, tzinfo=timezone.utc),
                            datetime(2025, 1, 1, 1, tzinfo=timezone.utc))
        # Force refresh — needs new payload queued
        fake_http.queue_kline_chunk(rows)
        calls_before = fake_http.call_count
        loader.fetch_klines("ETHUSDT", "5m", datetime(2025, 1, 1, tzinfo=timezone.utc),
                            datetime(2025, 1, 1, 1, tzinfo=timezone.utc), force_refresh=True)
        assert fake_http.call_count > calls_before

    def test_fetch_klines_empty_response(self, loader, fake_http):
        df = loader.fetch_klines("XYZ", "5m",
                                 datetime(2025, 1, 1, tzinfo=timezone.utc),
                                 datetime(2025, 1, 1, 1, tzinfo=timezone.utc))
        assert df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_fetch_klines_retry_on_transient_error(self, loader, fake_http):
        # First call raises, second succeeds
        rows = [[1735689600000, "3500.0", "3510.0", "3490.0", "3505.0", "100.0", "350000.0"]]
        fake_http.queue_kline_chunk(rows)
        fake_http.raise_on_call = 1
        df = loader.fetch_klines("ETHUSDT", "5m",
                                 datetime(2025, 1, 1, tzinfo=timezone.utc),
                                 datetime(2025, 1, 1, 1, tzinfo=timezone.utc))
        # We got data from the retry
        assert len(df) == 1


class TestFundingFetch:
    def test_fetch_funding_basic(self, loader, fake_http):
        rows = [
            {"fundingRate": "0.0001", "fundingRateTimestamp": 1735689600000},
            {"fundingRate": "0.0002", "fundingRateTimestamp": 1735718400000},
        ]
        fake_http.queue_funding_chunk(rows)
        df = loader.fetch_funding_history(
            "ETHUSDT",
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        assert not df.empty
        assert "funding_rate" in df.columns
        assert df["funding_rate"].iloc[0] == pytest.approx(0.0001)

    def test_fetch_funding_caches(self, loader, fake_http):
        rows = [{"fundingRate": "0.0001", "fundingRateTimestamp": 1735689600000}]
        fake_http.queue_funding_chunk(rows)
        loader.fetch_funding_history("ETHUSDT",
                                     datetime(2025, 1, 1, tzinfo=timezone.utc),
                                     datetime(2025, 1, 2, tzinfo=timezone.utc))
        calls_before = fake_http.call_count
        loader.fetch_funding_history("ETHUSDT",
                                     datetime(2025, 1, 1, tzinfo=timezone.utc),
                                     datetime(2025, 1, 2, tzinfo=timezone.utc))
        assert fake_http.call_count == calls_before                # cache hit


class TestCacheFormat:
    def test_cache_file_is_gzipped_csv(self, loader, fake_http, tmp_cache_dir):
        rows = [[1735689600000, "3500.0", "3510.0", "3490.0", "3505.0", "100.0", "350000.0"]]
        fake_http.queue_kline_chunk(rows)
        loader.fetch_klines("ETHUSDT", "5m",
                            datetime(2025, 1, 1, tzinfo=timezone.utc),
                            datetime(2025, 1, 1, 1, tzinfo=timezone.utc))
        path = list((tmp_cache_dir / "ETHUSDT" / "5m").glob("*.csv.gz"))
        assert len(path) == 1
        # Verify it actually unzips
        with gzip.open(path[0], "rt") as f:
            content = f.read()
        assert "close" in content


class TestAdvEstimate:
    def test_estimate_adv_24h_usd(self, loader, sample_klines):
        adv = loader.estimate_adv_24h_usd(sample_klines)
        assert adv > 0

    def test_estimate_adv_empty(self, loader):
        adv = loader.estimate_adv_24h_usd(pd.DataFrame())
        assert adv == 0.0
