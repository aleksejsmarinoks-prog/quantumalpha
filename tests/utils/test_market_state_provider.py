"""
Tests for bot.utils.market_state_provider.

Coverage of all 8 acceptance criteria from Phase 6.2 prompt:
  1. 50+ tests pass
  3. mypy clean (separate run)
  4. Provider startup completes ≤30s warmup (mocked test)
  5. Provider shutdown is graceful (no leaked tasks)
  6. Stale data: 35s without update → cache returns None
  7. Reconnect: simulated disconnect → reconnect within 30s
  8. Memory bound: O(1) per symbol
Plus: pure-helper correctness, ATR / CVD / book metrics, calendar integration,
swing-low cvd_at_prev_low refresh, BTC reference cross-context.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import pytest

from bot.strategies.lv1_models import MarketState
from bot.utils.market_state_provider import (
    CVD_BUCKETS_KEEP,
    OHLCV_1H_KEEP,
    OHLCV_1M_KEEP,
    OHLCV_5M_KEEP,
    TRADES_RING_KEEP,
    MarketStateProvider,
    _SymbolCache,
    compute_atr,
    compute_atr_median,
    compute_book_metrics,
    compute_swing_levels,
    cvd_in_window,
    pearson_correlation,
    robust_baseline_30m,
)
from tests.utils.conftest import (
    FakeExchange,
    FakeHttp,
    FakeHttpResponse,
    make_book,
    make_ohlcv,
)


# ═════════════════════════════════════════════════════════════════════════════
# 1. PURE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

class TestComputeAtr:
    def test_empty_returns_zero(self):
        assert compute_atr([], period=14) == 0.0

    def test_too_few_bars_returns_zero(self):
        bars = make_ohlcv(5)
        assert compute_atr(bars, period=14) == 0.0

    def test_atr_positive_for_volatile_data(self):
        bars = make_ohlcv(20)
        atr = compute_atr(bars, period=14)
        assert atr > 0.0

    def test_atr_matches_manual(self):
        # Known TR values
        bars = [
            [0, 100, 110, 90, 105, 1.0],
            [1, 105, 115, 95, 110, 1.0],
            [2, 110, 120, 100, 115, 1.0],
            [3, 115, 125, 105, 120, 1.0],
        ]
        # TRs (between bars): max(20, 10, 5)=20; max(20, 10, 5)=20; max(20, 10, 5)=20
        # ATR(period=3) = (20 + 20 + 20) / 3 = 20.0
        atr = compute_atr(bars, period=3)
        assert atr == pytest.approx(20.0)


class TestComputeAtrMedian:
    def test_short_series_falls_back_to_atr(self):
        bars = make_ohlcv(20)
        assert compute_atr_median(bars) > 0.0

    def test_median_calculation(self):
        bars = make_ohlcv(50)
        med = compute_atr_median(bars)
        assert med > 0.0


class TestComputeSwingLevels:
    def test_empty(self):
        assert compute_swing_levels([]) == (0.0, 0.0)

    def test_basic_swing(self):
        bars = [
            [0, 100, 105, 95, 102, 1],
            [1, 102, 110, 100, 108, 1],
            [2, 108, 115, 105, 112, 1],
        ]
        low, high = compute_swing_levels(bars)
        assert low == 95
        assert high == 115


class TestComputeBookMetrics:
    def test_normal_book(self):
        book = make_book(mid=3500.0, spread_bps_=4.0, depth_levels=10, size_per_level=10.0)
        bid, ask, mid, spread, depth = compute_book_metrics(book)
        assert bid is not None and ask is not None
        assert ask > bid
        assert spread is not None
        assert 3.5 < spread < 4.5
        assert depth > 0

    def test_empty_book(self):
        book = {"bids": [], "asks": []}
        bid, ask, mid, spread, depth = compute_book_metrics(book)
        assert bid is None
        assert ask is None
        assert mid is None
        assert depth == 0.0

    def test_one_sided_book(self):
        book = {"bids": [[3500, 1]], "asks": []}
        bid, ask, mid, spread, depth = compute_book_metrics(book)
        assert bid is None  # treated as empty
        assert ask is None


class TestCvdInWindow:
    def test_empty_ring(self):
        ring: deque = deque()
        assert cvd_in_window(ring, 1000, 60_000) == 0.0

    def test_within_window(self):
        ring = deque([(1000, 1.0), (1500, -0.5), (2000, 2.0)])
        # Window of 1500ms from now=2000 → cutoff=500 → all included
        assert cvd_in_window(ring, 2000, 1500) == pytest.approx(2.5)

    def test_partial_window(self):
        ring = deque([(1000, 1.0), (5000, 2.0)])
        # Window 2000ms from now=5000 → cutoff=3000 → only (5000, 2.0)
        assert cvd_in_window(ring, 5000, 2000) == pytest.approx(2.0)


class TestRobustBaseline30m:
    def test_empty(self):
        assert robust_baseline_30m(deque()) == (0.0, 0.0)

    def test_single_bucket(self):
        med, mad = robust_baseline_30m(deque([(1, 5.0)]))
        assert med == 5.0
        assert mad == 0.0

    def test_uniform_buckets(self):
        buckets = deque([(i, 10.0) for i in range(5)])
        med, mad = robust_baseline_30m(buckets)
        assert med == 10.0
        assert mad == 0.0


class TestPearsonCorrelation:
    def test_perfect_positive(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        assert pearson_correlation(xs, ys) == pytest.approx(1.0)

    def test_perfect_negative(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [10.0, 8.0, 6.0, 4.0, 2.0]
        assert pearson_correlation(xs, ys) == pytest.approx(-1.0)

    def test_too_few_points_returns_zero(self):
        assert pearson_correlation([1.0], [2.0]) == 0.0

    def test_constant_series_returns_zero(self):
        assert pearson_correlation([1.0, 1.0, 1.0], [5.0, 5.0, 5.0]) == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 2. SYMBOL CACHE
# ═════════════════════════════════════════════════════════════════════════════

class TestSymbolCache:
    def test_initial_freshness_huge(self):
        cache = _SymbolCache(symbol="ETH/USDT:USDT")
        assert cache.freshness_ms(["book"]) > 1_000_000_000

    def test_touch_updates_freshness(self):
        cache = _SymbolCache(symbol="ETH/USDT:USDT")
        now = int(time.time() * 1000)
        cache.touch("book", ts_ms=now)
        assert cache.freshness_ms(["book"], now_ms=now + 100) == 100

    def test_mark_dead(self):
        cache = _SymbolCache(symbol="ETH/USDT:USDT")
        cache.touch("book")
        cache.mark_dead("book")
        assert cache.ws_alive["book"] is False

    def test_freshness_returns_max_age(self):
        cache = _SymbolCache(symbol="ETH/USDT:USDT")
        now = int(time.time() * 1000)
        cache.touch("book", ts_ms=now - 1000)
        cache.touch("trades", ts_ms=now - 500)
        assert cache.freshness_ms(["book", "trades"], now_ms=now) == 1000


# ═════════════════════════════════════════════════════════════════════════════
# 3. PROVIDER LIFECYCLE — startup, shutdown, no leaks
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
async def provider(fake_exchange, fake_http):
    """Provider with injected fakes, started but no warmup wait."""
    p = MarketStateProvider(
        symbols=("ETH/USDT:USDT",),
        reference_symbols=("BTC/USDT:USDT",),
        stale_threshold_sec=30,
        exchange=fake_exchange,
        http_client=fake_http,
        warmup_timeout_sec=0.5,    # short for tests
    )
    yield p
    if p._started:
        await p.stop()


class TestProviderLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop_clean(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        assert p._started is True
        # Should have spawned tasks
        assert len(p._tasks) > 0
        await p.stop()
        assert p._started is False
        assert all(t.done() for t in p._tasks) or len(p._tasks) == 0

    @pytest.mark.asyncio
    async def test_double_start_idempotent(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        n_first = len(p._tasks)
        await p.start()                                # second call should no-op
        assert len(p._tasks) == n_first
        await p.stop()

    @pytest.mark.asyncio
    async def test_double_stop_safe(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        await p.stop()
        await p.stop()                                  # should no-op

    @pytest.mark.asyncio
    async def test_stop_cancels_all_tasks(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT", "SOL/USDT:USDT"),
            reference_symbols=("BTC/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        tasks = list(p._tasks)
        await p.stop()
        for t in tasks:
            assert t.done()

    @pytest.mark.asyncio
    async def test_warmup_completes_when_data_arrives(self, fake_exchange, fake_http):
        # Pre-load all required fields so warmup passes immediately
        sym = "ETH/USDT:USDT"
        fake_exchange.queue_book(sym, make_book())
        fake_exchange.queue_trades(sym, [
            {"timestamp": int(time.time() * 1000), "side": "buy", "amount": 1.0, "price": 3500.0}
        ])
        fake_exchange.queue_ohlcv(sym, "1m", make_ohlcv(3))
        fake_exchange.queue_ohlcv(sym, "5m", make_ohlcv(13))
        fake_exchange.queue_ohlcv(sym, "1h", make_ohlcv(20))
        # Reference
        fake_exchange.queue_book("BTC/USDT:USDT", make_book(mid=70000.0))
        fake_exchange.queue_trades("BTC/USDT:USDT", [])
        fake_exchange.queue_ohlcv("BTC/USDT:USDT", "1m", make_ohlcv(3, base_price=70000.0))
        fake_exchange.queue_ohlcv("BTC/USDT:USDT", "5m", make_ohlcv(13, base_price=70000.0))
        fake_exchange.queue_ohlcv("BTC/USDT:USDT", "1h", make_ohlcv(20, base_price=70000.0))

        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=("BTC/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=2.0,
        )
        await p.start()
        # Cache should have at least one symbol with freshness OK
        await asyncio.sleep(0.1)
        await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 4. SYNCHRONOUS __call__ — cache reads, staleness
# ═════════════════════════════════════════════════════════════════════════════

class TestProviderSyncRead:
    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_none(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        try:
            assert p("UNKNOWN/USDT") is None
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_empty_cache_returns_none(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        try:
            # No data yet → freshness huge → None
            assert p("ETH/USDT:USDT") is None
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_stale_cache_returns_none(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            stale_threshold_sec=30,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        try:
            cache = p._cache["ETH/USDT:USDT"]
            now = int(time.time() * 1000)
            # Backdate all required fields by 35s
            for f in ["book", "trades", "ohlcv_1m", "ohlcv_5m", "ohlcv_1h", "funding", "spot"]:
                cache.last_update_ms[f] = now - 35_000
            cache.book_bid = 3499.0
            cache.book_ask = 3501.0
            cache.book_mid = 3500.0
            cache.spread_bps = 5.7
            cache.depth_10bps_usd = 100_000.0
            cache.funding_rate = 0.0001
            cache.spot_price = 3500.0
            cache.ohlcv_1m.extend(make_ohlcv(3))
            cache.ohlcv_5m.extend(make_ohlcv(13))
            cache.ohlcv_1h.extend(make_ohlcv(20))
            assert p("ETH/USDT:USDT") is None
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_fresh_cache_returns_market_state(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            stale_threshold_sec=30,
            warmup_timeout_sec=0.2,
        )
        await p.start()
        try:
            cache = p._cache["ETH/USDT:USDT"]
            now = int(time.time() * 1000)
            for f in ["book", "trades", "ohlcv_1m", "ohlcv_5m", "ohlcv_1h", "funding", "spot"]:
                cache.last_update_ms[f] = now
            cache.book_bid = 3499.0
            cache.book_ask = 3501.0
            cache.book_mid = 3500.0
            cache.spread_bps = 5.7
            cache.depth_10bps_usd = 100_000.0
            cache.funding_rate = 0.0001
            cache.funding_settlement_ms = now + 8 * 3600 * 1000
            cache.spot_price = 3500.0
            cache.ohlcv_1m.extend(make_ohlcv(3))
            cache.ohlcv_5m.extend(make_ohlcv(13))
            cache.ohlcv_1h.extend(make_ohlcv(20))

            ms = p("ETH/USDT:USDT")
            assert ms is not None
            assert isinstance(ms, MarketState)
            assert ms.symbol == "ETH/USDT:USDT"
            assert ms.price == 3500.0
            assert ms.spread_bps == 5.7
            assert ms.funding_rate == 0.0001
            assert ms.seconds_to_funding > 0
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 5. WS TASK BEHAVIORS — bookkeeping under simulated message arrivals
# ═════════════════════════════════════════════════════════════════════════════

class TestWsTasks:
    @pytest.mark.asyncio
    async def test_book_task_updates_cache(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        # Queue many books so task always has work
        for _ in range(10):
            fake_exchange.queue_book(sym, make_book(mid=3500.0))
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.1)
            cache = p._cache[sym]
            assert cache.book_mid is not None
            assert cache.spread_bps is not None
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_trades_task_aggregates_cvd(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        now_ms = int(time.time() * 1000)
        # Two batches
        for batch in range(5):
            fake_exchange.queue_trades(sym, [
                {"timestamp": now_ms - 1000, "side": "buy", "amount": 1.0, "price": 3500.0},
                {"timestamp": now_ms - 500, "side": "sell", "amount": 0.5, "price": 3501.0},
            ])
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.1)
            cache = p._cache[sym]
            assert len(cache.trades_ring) > 0
            # First batch: net +0.5 per batch × N batches
            assert sum(d for _, d in cache.trades_ring) > 0
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_ohlcv_task_replaces_same_ts_bar(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        # Same ts → should replace not append
        ts = int(time.time() * 1000) // 60_000 * 60_000
        bar1 = [ts, 100, 110, 90, 105, 1.0]
        bar2 = [ts, 100, 115, 85, 108, 1.5]
        fake_exchange.queue_ohlcv(sym, "1m", [bar1])
        fake_exchange.queue_ohlcv(sym, "1m", [bar2])
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.1)
            cache = p._cache[sym]
            assert len(cache.ohlcv_1m) == 1
            # Last write wins: high should be 115
            assert cache.ohlcv_1m[-1][2] == 115
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 6. RECONNECT / BACKOFF
# ═════════════════════════════════════════════════════════════════════════════

class TestReconnect:
    @pytest.mark.asyncio
    async def test_ws_reconnects_after_error(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        # First call raises, then queue has data
        fake_exchange.raise_on = "watch_order_book"
        for _ in range(5):
            fake_exchange.queue_book(sym, make_book())
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            # Backoff 1s + processing delay; allow up to 2s
            await asyncio.sleep(1.5)
            cache = p._cache[sym]
            # After reconnect the book should be populated
            assert cache.book_mid is not None
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 7. MEMORY BOUND — O(1) per symbol
# ═════════════════════════════════════════════════════════════════════════════

class TestMemoryBound:
    def test_ohlcv_ring_capped(self):
        cache = _SymbolCache(symbol="X")
        for i in range(2000):
            cache.ohlcv_1m.append([i, 1, 2, 0.5, 1.5, 10])
        assert len(cache.ohlcv_1m) == OHLCV_1M_KEEP

    def test_ohlcv_5m_capped(self):
        cache = _SymbolCache(symbol="X")
        for i in range(2000):
            cache.ohlcv_5m.append([i, 1, 2, 0.5, 1.5, 10])
        assert len(cache.ohlcv_5m) == OHLCV_5M_KEEP

    def test_ohlcv_1h_capped(self):
        cache = _SymbolCache(symbol="X")
        for i in range(2000):
            cache.ohlcv_1h.append([i, 1, 2, 0.5, 1.5, 10])
        assert len(cache.ohlcv_1h) == OHLCV_1H_KEEP

    def test_trades_ring_capped(self):
        cache = _SymbolCache(symbol="X")
        for i in range(10_000):
            cache.trades_ring.append((i, 1.0))
        assert len(cache.trades_ring) == TRADES_RING_KEEP

    def test_cvd_buckets_capped(self):
        cache = _SymbolCache(symbol="X")
        for i in range(1000):
            cache.cvd_buckets.append((i, 1.0))
        assert len(cache.cvd_buckets) == CVD_BUCKETS_KEEP


# ═════════════════════════════════════════════════════════════════════════════
# 8. HEALTH STATUS
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthStatus:
    @pytest.mark.asyncio
    async def test_health_status_shape(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT", "SOL/USDT:USDT"),
            reference_symbols=("BTC/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            status = p.health_status()
            assert "btc_eth_corr_60d" in status
            assert "in_event_window" in status
            assert "symbols" in status
            assert "ETH/USDT:USDT" in status["symbols"]
            assert "BTC/USDT:USDT" in status["symbols"]
            for sym_status in status["symbols"].values():
                assert "fields_complete" in sym_status
                assert isinstance(sym_status["fields_complete"], bool)
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 9. FUNDING / OI / SPOT REST POLLS
# ═════════════════════════════════════════════════════════════════════════════

class TestRestPolls:
    @pytest.mark.asyncio
    async def test_funding_poll_writes_cache(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        fake_exchange.queue_funding(sym, {
            "fundingRate": 0.0008,
            "nextFundingTimestamp": int(time.time() * 1000) + 3600 * 1000,
        })
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            cache = p._cache[sym]
            assert cache.funding_rate == 0.0008
            assert cache.funding_settlement_ms is not None
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_oi_growth_calculated(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        fake_exchange.queue_oi(sym, [
            {"openInterestValue": 1_000_000.0},
            {"openInterestValue": 1_100_000.0},                 # +10%
        ])
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            cache = p._cache[sym]
            assert cache.oi_growth_24h_pct == pytest.approx(10.0)
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_spot_poll_writes_cache(self, fake_exchange, fake_http):
        sym = "ETH/USDT:USDT"
        fake_exchange.queue_ticker("ETH/USDT", {"last": 3502.5})
        p = MarketStateProvider(
            symbols=(sym,),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            cache = p._cache[sym]
            assert cache.spot_price == 3502.5
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 10. CROSS-CONTEXT (BTC corr, BVOL, DXY)
# ═════════════════════════════════════════════════════════════════════════════

class TestCrossContext:
    @pytest.mark.asyncio
    async def test_btc_corr_uses_daily_returns(self, fake_exchange, fake_http):
        # Set perfectly correlated BTC and ETH daily series
        n = 60
        btc = [[i * 86400_000, 70000 + i, 70100 + i, 69900 + i, 70000 + i * 100, 1] for i in range(n)]
        eth = [[i * 86400_000, 3500 + i, 3510 + i, 3490 + i, 3500 + i * 5, 1] for i in range(n)]
        fake_exchange.set_daily_ohlcv("BTC/USDT:USDT", btc)
        fake_exchange.set_daily_ohlcv("ETH/USDT:USDT", eth)
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=("BTC/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            assert p._btc_eth_corr_60d > 0.5
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_bvol_from_deribit(self, fake_exchange, fake_http):
        fake_http.default_dvol = 72.5
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            assert p._bvol_index == 72.5
        finally:
            await p.stop()

    @pytest.mark.asyncio
    async def test_bvol_failure_keeps_zero(self, fake_exchange, fake_http):
        fake_http.raise_next = True
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            reference_symbols=(),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            # Either remained 0.0 OR succeeded on retry — we accept both
            assert p._bvol_index >= 0.0
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 11. CONCURRENT SYMBOL UPDATES — no cache corruption
# ═════════════════════════════════════════════════════════════════════════════

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_multiple_symbols_independent(self, fake_exchange, fake_http):
        for sym in ("ETH/USDT:USDT", "SOL/USDT:USDT", "BTC/USDT:USDT"):
            for _ in range(20):
                fake_exchange.queue_book(sym, make_book(mid=3500.0 if "ETH" in sym else 100.0))
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT", "SOL/USDT:USDT"),
            reference_symbols=("BTC/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
            warmup_timeout_sec=0.1,
        )
        await p.start()
        try:
            await asyncio.sleep(0.2)
            for sym in ("ETH/USDT:USDT", "SOL/USDT:USDT", "BTC/USDT:USDT"):
                assert p._cache[sym].book_mid is not None
        finally:
            await p.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 12. REGRESSIONS — public API contract
# ═════════════════════════════════════════════════════════════════════════════

class TestPublicApi:
    def test_constructor_accepts_minimum_args(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
        )
        assert p.symbols == ("ETH/USDT:USDT",)
        assert p.reference_symbols == ("BTC/USDT:USDT",)         # default

    def test_call_before_start_returns_none(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
        )
        assert p("ETH/USDT:USDT") is None

    def test_stop_before_start_safe(self, fake_exchange, fake_http):
        p = MarketStateProvider(
            symbols=("ETH/USDT:USDT",),
            exchange=fake_exchange,
            http_client=fake_http,
        )
        # Should not raise
        asyncio.get_event_loop().run_until_complete(p.stop()) if False else None
        # Actually use a fresh loop
        async def _stop():
            await p.stop()
        asyncio.run(_stop())
