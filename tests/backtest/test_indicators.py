"""IndicatorsProvider tests (Phase 6.3.1a Step 5a).

Critical focus: anti-lookahead invariant for resample + RSI computation.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import Bar, IndicatorsProvider, IndicatorsConfig


UTC = timezone.utc
T0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


def _bar(idx: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Bar:
    return Bar(
        timestamp=T0 + timedelta(minutes=5 * idx),
        open=o, high=h, low=l, close=c, volume=v,
    )


def _flat_bars(n: int, price: float = 2000.0) -> List[Bar]:
    return [_bar(i, price, price + 0.5, price - 0.5, price) for i in range(n)]


def _trending_bars(n: int, base: float = 2000.0, slope: float = 1.0) -> List[Bar]:
    """n bars trending up by `slope` per bar."""
    bars = []
    price = base
    for i in range(n):
        bars.append(_bar(i, price, price + slope, price - 0.1, price + slope))
        price += slope
    return bars


# ===========================================================================
# Basic init + edge cases
# ===========================================================================

class TestConstruction:

    def test_empty_bars_raises(self):
        with pytest.raises(ValueError, match="at least 1 bar"):
            IndicatorsProvider([])

    def test_invalid_rsi_period(self):
        with pytest.raises(ValueError, match="rsi_period"):
            IndicatorsConfig(rsi_period=1)

    def test_invalid_resample_minutes(self):
        with pytest.raises(ValueError, match="resample_minutes"):
            IndicatorsConfig(resample_minutes=0)


# ===========================================================================
# last_price + warmup behavior
# ===========================================================================

class TestLastPrice:

    def test_last_price_after_one_bar(self):
        bars = _flat_bars(2)
        ind = IndicatorsProvider(bars)
        # Query at bar 1's timestamp + 1 min — should see bar 0's close
        ts = bars[0].timestamp + timedelta(seconds=30)
        result = ind.indicators_at(ts)
        # Note: bisect_left returns strictly-before
        # Actually ts > bars[0].timestamp → idx_5m=1 → bars[0] visible → last_price=2000
        assert result["last_price"] == 2000

    def test_no_bars_before_query_empty_result(self):
        bars = _flat_bars(5)
        ind = IndicatorsProvider(bars)
        # Query at T0 (before any bar — bars[0] has timestamp T0+5min)
        result = ind.indicators_at(T0)
        assert result == {}

    def test_query_at_first_bar_timestamp_strictly_before(self):
        """Query at bars[0].timestamp → bars[0] is NOT strictly before → empty."""
        bars = _flat_bars(5)
        ind = IndicatorsProvider(bars)
        result = ind.indicators_at(bars[0].timestamp)
        assert result == {}


# ===========================================================================
# 1h resample correctness
# ===========================================================================

class TestResample:

    def test_resample_groups_by_hour(self):
        """5m bars within 10:05-11:00 window → single 1h bar at 11:00."""
        # bars starting AFTER 10:00 (10:05, 10:10, ..., 10:55, 11:00)
        # All fall into the window ending at 11:00 → ONE 1h bar
        bars = [
            Bar(timestamp=T0 + timedelta(minutes=5 + 5 * i),
                open=2000, high=2001, low=1999, close=2000, volume=1000)
            for i in range(12)
        ]
        ind = IndicatorsProvider(bars)
        assert len(ind._h1_bars) == 1
        # 1h bar has timestamp = window end = 11:00
        assert ind._h1_bars[0].timestamp == T0 + timedelta(hours=1)

    def test_resample_first_bar_on_hour_boundary_separate_window(self):
        """Bar AT 10:00 (T0) belongs to window 10:00 (covers 9:00-10:00),
        bar at 10:05 belongs to window 11:00. → TWO h1 bars."""
        bars = _flat_bars(12)   # bars[0] @ 10:00, bars[1..11] @ 10:05..10:55
        ind = IndicatorsProvider(bars)
        assert len(ind._h1_bars) == 2

    def test_resample_aggregates_ohlc_correctly(self):
        # Bar 0: O=2000 H=2010 L=1990 C=2005
        # Bar 1: O=2005 H=2020 L=2000 C=2015
        # 1h bar should be: O=2000 H=2020 L=1990 C=2015 V=summed
        bars = [
            Bar(timestamp=T0 + timedelta(minutes=5),
                open=2000, high=2010, low=1990, close=2005, volume=100),
            Bar(timestamp=T0 + timedelta(minutes=10),
                open=2005, high=2020, low=2000, close=2015, volume=200),
        ]
        ind = IndicatorsProvider(bars)
        h1 = ind._h1_bars[0]
        assert h1.open == 2000
        assert h1.high == 2020
        assert h1.low == 1990
        assert h1.close == 2015
        assert h1.volume == 300


# ===========================================================================
# RSI computation
# ===========================================================================

class TestRSI:

    def test_rsi_insufficient_history(self):
        """Need 15+ complete 1h bars → < 15h of 5m data → None."""
        bars = _flat_bars(48)   # 48 × 5m = 4h → 4 complete 1h bars
        ind = IndicatorsProvider(bars)
        ts = bars[-1].timestamp + timedelta(hours=1)
        result = ind.indicators_at(ts)
        assert "rsi_14_1h" not in result

    def test_rsi_on_flat_market_is_undefined(self):
        """Constant price → RSI undefined (no gains, no losses)."""
        bars = _flat_bars(250)  # ~20h
        ind = IndicatorsProvider(bars)
        ts = bars[-1].timestamp + timedelta(hours=1)
        result = ind.indicators_at(ts)
        # RSI on absolutely flat market is undefined → should not be present
        assert "rsi_14_1h" not in result

    def test_rsi_on_strongly_uptrending_market(self):
        bars = _trending_bars(250, slope=2.0)   # consistently up
        ind = IndicatorsProvider(bars)
        ts = bars[-1].timestamp + timedelta(hours=1)
        rsi = ind.indicators_at(ts).get("rsi_14_1h")
        assert rsi is not None
        assert rsi > 70.0   # overbought signal expected

    def test_rsi_on_strongly_downtrending_market(self):
        bars = _trending_bars(250, slope=-2.0)
        ind = IndicatorsProvider(bars)
        ts = bars[-1].timestamp + timedelta(hours=1)
        rsi = ind.indicators_at(ts).get("rsi_14_1h")
        assert rsi is not None
        assert rsi < 30.0   # oversold signal expected


# ===========================================================================
# Anti-lookahead invariant — THE critical property
# ===========================================================================

class TestAntiLookahead:

    def test_indicator_at_bar_excludes_that_bar(self):
        """At ts == bars[k].timestamp, bars[k] is NOT visible (strict <).
        At ts == bars[k].timestamp + 1µs, bars[k] IS visible.
        Verified via last_price (deterministic).
        """
        # Each bar has DISTINCT close price (idx*10) so last_price differs per bar
        bars = [
            Bar(timestamp=T0 + timedelta(minutes=5 * i),
                open=2000 + i * 10, high=2010 + i * 10,
                low=1990 + i * 10, close=2000 + i * 10, volume=1000)
            for i in range(50)
        ]
        ind = IndicatorsProvider(bars)

        # Query at bars[10].timestamp — bars[10] is NOT visible → last_price = bars[9].close
        ts_at_10 = bars[10].timestamp
        result_at = ind.indicators_at(ts_at_10)
        assert result_at["last_price"] == bars[9].close

        # Query 1 microsecond AFTER bars[10].timestamp — bars[10] IS visible → last_price = bars[10].close
        ts_after_10 = bars[10].timestamp + timedelta(microseconds=1)
        result_after = ind.indicators_at(ts_after_10)
        assert result_after["last_price"] == bars[10].close

        # Definitive: the two prices differ
        assert result_at["last_price"] != result_after["last_price"]

    def test_query_strict_less_than(self):
        """Query AT bar timestamp → that bar NOT visible. Query 1µs after → visible."""
        bars = _flat_bars(250)
        ind = IndicatorsProvider(bars)

        ts_at = bars[-1].timestamp
        ts_after = bars[-1].timestamp + timedelta(microseconds=1)

        result_at = ind.indicators_at(ts_at)
        result_after = ind.indicators_at(ts_after)

        # last_price differs by 1 bar
        # At ts_at: bars[-1] is NOT strictly before → last_price = bars[-2].close
        # At ts_after: bars[-1] IS strictly before → last_price = bars[-1].close
        # Both are 2000 in flat market — but the underlying idx differs
        assert result_at["last_price"] == result_after["last_price"]   # flat → equal
        # Cache should be different entries
        assert ts_at in ind._cache
        assert ts_after in ind._cache

    def test_caching(self):
        bars = _flat_bars(250)
        ind = IndicatorsProvider(bars)
        ts = bars[-1].timestamp + timedelta(hours=1)
        r1 = ind.indicators_at(ts)
        r2 = ind.indicators_at(ts)
        assert r1 is r2 or r1 == r2   # cached
        assert ts in ind._cache


# ===========================================================================
# Engine integration
# ===========================================================================

class TestEngineIntegration:

    def test_callable_for_engine(self):
        bars = _flat_bars(250)
        ind = IndicatorsProvider(bars)
        callable_ = ind.callable_for_engine()

        ts = bars[100].timestamp
        result_via_callable = callable_(ts, bars[100])
        result_direct = ind.indicators_at(ts)
        assert result_via_callable == result_direct

    def test_returns_1h_computation(self):
        """Trending up market → positive returns_1h."""
        bars = _trending_bars(250, slope=1.0)
        ind = IndicatorsProvider(bars)
        ts = bars[-1].timestamp + timedelta(hours=1)
        result = ind.indicators_at(ts)
        assert "returns_1h" in result
        assert result["returns_1h"] > 0
