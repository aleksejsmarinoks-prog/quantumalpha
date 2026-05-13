"""
LV1 — Market State Provider (Phase 6.2)
========================================

Live market-data aggregator using ccxt.pro WebSocket feeds plus periodic REST
polls. Maintains an in-memory cache of `MarketState` per symbol, fed by
background async tasks. The synchronous `__call__` method reads the cache
without blocking, allowing the LV1 strategy evaluation loop (driven by
APScheduler from a sync context) to fetch fresh state safely.

Architecture
------------
                    ┌──────────────────┐
                    │  ccxt.pro async  │
                    │  exchange object │
                    └────────┬─────────┘
                             │
   ┌─────────────────────────┼─────────────────────────────┐
   │                         │                             │
   ▼                         ▼                             ▼
 _ws_orderbook_task    _ws_trades_task              _ws_ohlcv_*_task
   (per symbol)         (per symbol)                 (per symbol, 3 TFs)
   │                         │                             │
   └──── writes to ──┬───────┴────── writes to ──┬─────────┘
                     ▼                            ▼
              _SymbolCache (per symbol, in-memory dataclass)
                     ▲
   periodic REST polls also write here:
   _funding_poll_task / _oi_poll_task / _spot_perp_poll_task /
   _btc_correlation_task / _dxy_task / _bvol_task / _calendar_task

`__call__(symbol)` (sync) reads cache → returns Optional[MarketState] or None
if stale. The strategy treats None as NO_MARKET_STATE and skips.

Reconnection
------------
Each WS task is wrapped in `while True: try / except / backoff`. Backoff
sequence: 1s → 2s → 4s → 8s → 16s → 30s (capped). Reset on successful
message receipt.

Memory bounds
-------------
All ring buffers are fixed-size:
- 1m OHLCV: 5 bars (only need 2 prev candles)
- 5m OHLCV: 12 bars (1h lookback for swings)
- 1h OHLCV: 720 bars (30d for atr_median_30d)
- CVD bucket buffer: 30 buckets × 60s = 30m window
- Trade-tick CVD: ring of last 1000 trades for 60s/15m windows

Cache size per symbol: ~50KB worst case.

Author: QuantForge / QuantumAlpha
Phase: 6.2
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Optional, Protocol

from bot.strategies.lv1_models import MarketState
from bot.utils.calendar_events import in_event_window


log = logging.getLogger("lv1.provider")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Window sizes
WINDOW_60S_MS = 60_000
WINDOW_15M_MS = 15 * 60_000
WINDOW_30M_MS = 30 * 60_000

# Ring buffer caps
OHLCV_1M_KEEP = 5
OHLCV_5M_KEEP = 12          # 12 × 5min = 1h swing-low/high lookback
OHLCV_1H_KEEP = 720         # 30 days for atr_median_30d
TRADES_RING_KEEP = 2000
CVD_BUCKETS_KEEP = 30       # 30 × 60s = 30 minute robust-z baseline

# Reconnect backoff
RECONNECT_BACKOFF_SEQ = (1, 2, 4, 8, 16, 30)

# REST poll intervals (seconds)
FUNDING_POLL_SEC = 60
OI_POLL_SEC = 300
SPOT_POLL_SEC = 10
BTC_CORR_POLL_SEC = 3600
DXY_POLL_SEC = 300
BVOL_POLL_SEC = 300
CALENDAR_POLL_SEC = 60

# WS task health
WS_HEALTH_DEAD_SEC = 60     # no message in 60s → ws_alive=False
DEGRADED_SYMBOLS_THRESHOLD = 3

# ATR period
ATR_PERIOD = 14


# ─────────────────────────────────────────────────────────────────────────────
# Protocols (for ccxt.pro / httpx duck typing)
# ─────────────────────────────────────────────────────────────────────────────

class CcxtProExchange(Protocol):
    """Subset of ccxt.pro Exchange used by provider."""

    async def watch_order_book(self, symbol: str, limit: int = 50) -> dict: ...
    async def watch_trades(self, symbol: str) -> list[dict]: ...
    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list]: ...
    async def fetch_funding_rate(self, symbol: str) -> dict: ...
    async def fetch_open_interest_history(self, symbol: str, timeframe: str = "1h", limit: int = 24) -> list[dict]: ...
    async def fetch_ticker(self, symbol: str) -> dict: ...
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list[list]: ...
    async def close(self) -> None: ...


class HttpxClient(Protocol):
    """Subset of httpx.AsyncClient used by provider for Deribit/DXY polling."""

    async def get(self, url: str, *, timeout: float = ...) -> Any: ...
    async def aclose(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# Cache dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _SymbolCache:
    """All state for a single symbol. All fields Optional during warmup."""
    symbol: str

    # Per-field staleness tracking (epoch ms)
    last_update_ms: dict[str, int] = field(default_factory=dict)
    ws_alive: dict[str, bool] = field(default_factory=dict)

    # Orderbook
    book_bid: Optional[float] = None
    book_ask: Optional[float] = None
    book_mid: Optional[float] = None
    spread_bps: Optional[float] = None
    depth_10bps_usd: Optional[float] = None

    # Trade stream → CVD
    trades_ring: Deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=TRADES_RING_KEEP))
    cvd_buckets: Deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=CVD_BUCKETS_KEEP))
    cvd_at_prev_low: float = 0.0
    last_swing_low_5m_seen: Optional[float] = None

    # OHLCV ring buffers (each entry: [ts_ms, open, high, low, close, volume])
    ohlcv_1m: Deque[list] = field(default_factory=lambda: deque(maxlen=OHLCV_1M_KEEP))
    ohlcv_5m: Deque[list] = field(default_factory=lambda: deque(maxlen=OHLCV_5M_KEEP))
    ohlcv_1h: Deque[list] = field(default_factory=lambda: deque(maxlen=OHLCV_1H_KEEP))

    # REST-polled fields
    funding_rate: Optional[float] = None
    funding_settlement_ms: Optional[int] = None        # next settlement epoch ms
    spot_price: Optional[float] = None
    oi_growth_24h_pct: Optional[float] = None

    # Update helpers ──────────────────────────────────────────────────────
    def touch(self, field_name: str, ts_ms: Optional[int] = None) -> None:
        self.last_update_ms[field_name] = int(ts_ms or (time.time() * 1000))
        self.ws_alive[field_name] = True

    def mark_dead(self, field_name: str) -> None:
        self.ws_alive[field_name] = False

    def freshness_ms(self, fields: list[str], now_ms: Optional[int] = None) -> int:
        """Worst-case staleness in ms across the listed fields."""
        if not self.last_update_ms:
            return 10**12
        now = int(now_ms or (time.time() * 1000))
        ages = []
        for f in fields:
            ts = self.last_update_ms.get(f)
            if ts is None:
                return 10**12
            ages.append(now - ts)
        return max(ages) if ages else 10**12


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (testable in isolation)
# ─────────────────────────────────────────────────────────────────────────────

def compute_atr(bars: list[list], period: int = ATR_PERIOD) -> float:
    """
    Wilder's-style ATR using simple moving average of true ranges over `period`.

    `bars`: list of [ts, open, high, low, close, volume].
    Returns 0.0 if fewer than period+1 bars available.
    """
    if len(bars) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = bars[i][2]
        low = bars[i][3]
        prev_close = bars[i - 1][4]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
    # Simple average of last `period` TRs
    return sum(trs[-period:]) / period


def compute_atr_median(bars: list[list], window_period: int = ATR_PERIOD) -> float:
    """
    Median ATR over rolling windows of length `window_period`.

    For each window, computes ATR; returns median. Used for atr_median_30d
    when fed last 720 1h bars.
    """
    if len(bars) < 2 * window_period:
        return compute_atr(bars, window_period)
    atrs: list[float] = []
    for i in range(window_period + 1, len(bars) + 1):
        sub = bars[i - window_period - 1: i]
        atrs.append(compute_atr(sub, window_period))
    if not atrs:
        return 0.0
    return statistics.median(atrs)


def compute_swing_levels(bars_5m: list[list]) -> tuple[float, float]:
    """Return (swing_low, swing_high) over the bars window. (0.0, 0.0) if empty."""
    if not bars_5m:
        return 0.0, 0.0
    swing_low = min(b[3] for b in bars_5m)
    swing_high = max(b[2] for b in bars_5m)
    return swing_low, swing_high


def compute_book_metrics(book: dict) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], float]:
    """
    From a CCXT-format book {'bids': [[p, s], ...], 'asks': [[p, s], ...]}, return:
        (best_bid, best_ask, mid, spread_bps, depth_10bps_usd)

    Any field returns None if the book side is empty.
    `depth_10bps_usd` is 0.0 if mid unavailable.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None, None, None, None, 0.0
    bid = float(bids[0][0])
    ask = float(asks[0][0])
    if bid <= 0:
        return bid, ask, None, None, 0.0
    mid = (bid + ask) / 2.0
    spread_bps = ((ask - bid) / mid) * 10_000.0
    # Depth within 10 bps of mid, both sides
    range_pct = 10.0 / 10_000.0
    floor = mid * (1.0 - range_pct)
    ceil = mid * (1.0 + range_pct)
    notional = 0.0
    for p, s in bids:
        pf, sf = float(p), float(s)
        if pf >= floor:
            notional += pf * sf
        else:
            break
    for p, s in asks:
        pf, sf = float(p), float(s)
        if pf <= ceil:
            notional += pf * sf
        else:
            break
    return bid, ask, mid, spread_bps, notional


def cvd_in_window(trades_ring: Deque[tuple[int, float]], now_ms: int, window_ms: int) -> float:
    """Sum signed deltas within `window_ms` ms of `now_ms`. Buy=+ Sell=-."""
    cutoff = now_ms - window_ms
    total = 0.0
    for ts, delta in reversed(trades_ring):
        if ts >= cutoff:
            total += delta
        else:
            break
    return total


def robust_baseline_30m(buckets: Deque[tuple[int, float]]) -> tuple[float, float]:
    """Return (median, MAD) of bucket values, or (0, 0) for empty."""
    if not buckets:
        return 0.0, 0.0
    vals = [v for _, v in buckets]
    med = statistics.median(vals)
    absdev = [abs(v - med) for v in vals]
    mad = statistics.median(absdev) if absdev else 0.0
    return med, mad


def pearson_correlation(xs: list[float], ys: list[float]) -> float:
    """Pearson r between two series of equal length. 0.0 on degenerate input."""
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs = xs[-n:]
    ys = ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / math.sqrt(vx * vy)


# ─────────────────────────────────────────────────────────────────────────────
# Main provider class
# ─────────────────────────────────────────────────────────────────────────────

class MarketStateProvider:
    """
    See module docstring. Constructor is sync; start()/stop() are async.
    `__call__(symbol)` is sync and never blocks.
    """

    def __init__(
        self,
        symbols: tuple[str, ...],
        reference_symbols: tuple[str, ...] = ("BTC/USDT:USDT",),
        exchange_name: str = "bybit",
        stale_threshold_sec: int = 30,
        exchange: Optional[CcxtProExchange] = None,
        spot_exchange: Optional[CcxtProExchange] = None,
        http_client: Optional[HttpxClient] = None,
        warmup_timeout_sec: float = 30.0,
        degraded_alert_callback: Optional[Callable[[list[str]], Awaitable[None]]] = None,
    ):
        self.symbols = tuple(symbols)
        self.reference_symbols = tuple(reference_symbols)
        self.exchange_name = exchange_name
        self.stale_threshold_sec = stale_threshold_sec
        self.warmup_timeout_sec = warmup_timeout_sec
        self.degraded_alert_callback = degraded_alert_callback

        # Caches: primary + reference
        all_syms = list(self.symbols) + list(self.reference_symbols)
        self._cache: dict[str, _SymbolCache] = {s: _SymbolCache(symbol=s) for s in all_syms}

        # Cross-context (shared across symbols)
        self._btc_eth_corr_60d: float = 0.85
        self._dxy_intraday_pct: float = 0.0
        self._dxy_session_open: Optional[float] = None
        self._bvol_index: float = 0.0

        # Injected services (None → lazy create on start())
        self._exchange = exchange
        self._spot_exchange = spot_exchange
        self._http = http_client
        self._owns_exchange = exchange is None
        self._owns_spot = spot_exchange is None
        self._owns_http = http_client is None

        # Async task management
        self._tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event = asyncio.Event()
        self._started: bool = False

        # Health monitor
        self._last_degraded_alert_ms: int = 0

    # ── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn background tasks and wait for warmup (≤warmup_timeout_sec)."""
        if self._started:
            return
        self._started = True
        self._stop_event.clear()

        # Lazy create services if not injected
        if self._exchange is None:
            self._exchange = await self._create_default_exchange()
        if self._http is None:
            self._http = await self._create_default_http()

        # Spawn tasks per symbol (primary + reference)
        all_syms = list(self.symbols) + list(self.reference_symbols)
        for sym in all_syms:
            self._tasks.append(asyncio.create_task(
                self._ws_orderbook_task(sym), name=f"ws_book_{sym}"))
            self._tasks.append(asyncio.create_task(
                self._ws_trades_task(sym), name=f"ws_trades_{sym}"))
            self._tasks.append(asyncio.create_task(
                self._ws_ohlcv_task(sym, "1m"), name=f"ws_ohlcv_1m_{sym}"))
            self._tasks.append(asyncio.create_task(
                self._ws_ohlcv_task(sym, "5m"), name=f"ws_ohlcv_5m_{sym}"))
            self._tasks.append(asyncio.create_task(
                self._ws_ohlcv_task(sym, "1h"), name=f"ws_ohlcv_1h_{sym}"))

        # Periodic REST polls (per symbol where needed)
        for sym in self.symbols:
            self._tasks.append(asyncio.create_task(
                self._funding_poll_task(sym), name=f"funding_{sym}"))
            self._tasks.append(asyncio.create_task(
                self._oi_poll_task(sym), name=f"oi_{sym}"))
            self._tasks.append(asyncio.create_task(
                self._spot_perp_poll_task(sym), name=f"spot_{sym}"))

        # Cross-context (one each)
        self._tasks.append(asyncio.create_task(self._btc_correlation_task(), name="btc_corr"))
        self._tasks.append(asyncio.create_task(self._dxy_task(), name="dxy"))
        self._tasks.append(asyncio.create_task(self._bvol_task(), name="bvol"))
        self._tasks.append(asyncio.create_task(self._health_monitor_task(), name="health"))

        # Wait for warmup
        try:
            await asyncio.wait_for(self._wait_warmup(), timeout=self.warmup_timeout_sec)
            log.info("provider warmup complete: %d symbols cached", len(self._cache))
        except asyncio.TimeoutError:
            log.warning("provider warmup TIMEOUT (%.1fs) — continuing in degraded state",
                        self.warmup_timeout_sec)

    async def stop(self) -> None:
        """Cancel all tasks and close owned services."""
        if not self._started:
            return
        self._stop_event.set()
        for t in self._tasks:
            t.cancel()
        # Drain
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Close owned services
        if self._owns_exchange and self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as e:
                log.warning("exchange close error: %s", e)
        if self._owns_http and self._http is not None:
            try:
                await self._http.aclose()
            except Exception as e:
                log.warning("http close error: %s", e)
        self._started = False
        log.info("provider stopped cleanly")

    def __call__(self, symbol: str) -> Optional[MarketState]:
        """Sync read from cache. Returns None if symbol unknown OR cache stale."""
        cache = self._cache.get(symbol)
        if cache is None:
            return None
        # Required field set for a usable MarketState
        required = [
            "book", "trades", "ohlcv_1m", "ohlcv_5m", "ohlcv_1h",
            "funding", "spot",
        ]
        stale_ms = cache.freshness_ms(required)
        if stale_ms > self.stale_threshold_sec * 1000:
            return None
        return self._build_market_state(cache)

    def health_status(self) -> dict[str, Any]:
        """Per-symbol diagnostics for /lv1_provider_status."""
        now_ms = int(time.time() * 1000)
        result: dict[str, Any] = {
            "btc_eth_corr_60d": self._btc_eth_corr_60d,
            "dxy_intraday_pct": self._dxy_intraday_pct,
            "bvol_index": self._bvol_index,
            "in_event_window": in_event_window(),
            "symbols": {},
        }
        for sym, cache in self._cache.items():
            ages_ms = {
                k: (now_ms - v) for k, v in cache.last_update_ms.items()
            }
            result["symbols"][sym] = {
                "last_update_ms": dict(cache.last_update_ms),
                "field_age_ms": ages_ms,
                "ws_alive": dict(cache.ws_alive),
                "fields_complete": cache.freshness_ms(
                    ["book", "trades", "ohlcv_1m", "ohlcv_5m", "ohlcv_1h", "funding", "spot"]
                ) <= self.stale_threshold_sec * 1000,
            }
        return result

    # ── MarketState assembly ────────────────────────────────────────────

    def _build_market_state(self, cache: _SymbolCache) -> Optional[MarketState]:
        # All required fields must be present
        if (
            cache.book_bid is None or cache.book_ask is None or cache.book_mid is None
            or cache.spread_bps is None or cache.depth_10bps_usd is None
            or cache.funding_rate is None or cache.spot_price is None
            or len(cache.ohlcv_1m) < 2 or len(cache.ohlcv_5m) < 1
            or len(cache.ohlcv_1h) < ATR_PERIOD + 1
        ):
            return None

        last_1m = cache.ohlcv_1m[-1]
        prev_1m = cache.ohlcv_1m[-2]
        swing_low, swing_high = compute_swing_levels(list(cache.ohlcv_5m))
        atr_5m = compute_atr(list(cache.ohlcv_5m))
        atr_1h_bars = list(cache.ohlcv_1h)
        atr_1h = compute_atr(atr_1h_bars)
        atr_med = compute_atr_median(atr_1h_bars)

        # CVD
        now_ms = int(time.time() * 1000)
        cvd_60s = cvd_in_window(cache.trades_ring, now_ms, WINDOW_60S_MS)
        cvd_15m = cvd_in_window(cache.trades_ring, now_ms, WINDOW_15M_MS)
        med_30m, mad_30m = robust_baseline_30m(cache.cvd_buckets)

        # Funding seconds
        sec_to_funding = 0
        if cache.funding_settlement_ms is not None:
            sec_to_funding = max(0, (cache.funding_settlement_ms - now_ms) // 1000)

        # Cross-context BTC ref
        btc_1m_return = self._btc_1m_return()

        return MarketState(
            symbol=cache.symbol,
            price=cache.book_mid,
            spot_price=cache.spot_price,
            funding_rate=cache.funding_rate,
            seconds_to_funding=int(sec_to_funding),
            atr_5m=atr_5m,
            atr_1h=atr_1h,
            atr_median_30d=atr_med,
            swing_low_5m=swing_low,
            swing_high_5m=swing_high,
            last_low_1m=last_1m[3],
            last_high_1m=last_1m[2],
            last_close_1m=last_1m[4],
            prev_low_1m=prev_1m[3],
            prev_high_1m=prev_1m[2],
            prev_close_1m=prev_1m[4],
            cvd_15m=cvd_15m,
            cvd_15m_at_prev_low=cache.cvd_at_prev_low,
            cvd_60s=cvd_60s,
            cvd_rolling_median_30m=med_30m,
            cvd_rolling_mad_30m=mad_30m,
            spread_bps=cache.spread_bps,
            depth_10bps_usd=cache.depth_10bps_usd,
            book_bid=cache.book_bid,
            book_ask=cache.book_ask,
            btc_1m_return=btc_1m_return,
            btc_eth_corr_60d=self._btc_eth_corr_60d,
            oi_growth_24h_pct=cache.oi_growth_24h_pct or 0.0,
            bvol_index=self._bvol_index,
            dxy_intraday_pct=self._dxy_intraday_pct,
            in_calendar_event_window=in_event_window(),
            timestamp=datetime.now(timezone.utc),
        )

    def _btc_1m_return(self) -> float:
        for ref in self.reference_symbols:
            cache = self._cache.get(ref)
            if cache is None or len(cache.ohlcv_1m) < 2:
                continue
            prev = cache.ohlcv_1m[-2][4]
            last = cache.ohlcv_1m[-1][4]
            if prev > 0:
                return (last - prev) / prev
        return 0.0

    # ── Lazy service factories (overridable for tests) ──────────────────

    async def _create_default_exchange(self) -> CcxtProExchange:
        """Default ccxt.pro exchange. Tests inject their own."""
        try:
            import ccxt.pro as ccxtpro                              # type: ignore[import]
        except ImportError as e:
            raise RuntimeError("ccxt.pro is required but not installed") from e
        cls = getattr(ccxtpro, self.exchange_name)
        ex = cls({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        return ex

    async def _create_default_http(self) -> HttpxClient:
        try:
            import httpx                                             # type: ignore[import]
        except ImportError as e:
            raise RuntimeError("httpx is required but not installed") from e
        return httpx.AsyncClient(timeout=10.0)

    # ── WS tasks ────────────────────────────────────────────────────────

    async def _backoff_loop(self, name: str, body: Callable[[], Awaitable[None]]) -> None:
        """Run `body()` in an infinite loop with exponential backoff on errors."""
        idx = 0
        while not self._stop_event.is_set():
            try:
                await body()
                idx = 0                                               # success → reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:
                wait = RECONNECT_BACKOFF_SEQ[min(idx, len(RECONNECT_BACKOFF_SEQ) - 1)]
                log.warning("%s task error (retry in %ds): %s", name, wait, e)
                idx += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _ws_orderbook_task(self, symbol: str) -> None:
        cache = self._cache[symbol]

        async def body() -> None:
            assert self._exchange is not None
            book = await self._exchange.watch_order_book(symbol, limit=50)
            bid, ask, mid, spread, depth = compute_book_metrics(book)
            if bid is not None:
                cache.book_bid = bid
            if ask is not None:
                cache.book_ask = ask
            cache.book_mid = mid
            cache.spread_bps = spread
            cache.depth_10bps_usd = depth
            cache.touch("book")

        await self._backoff_loop(f"orderbook[{symbol}]", body)

    async def _ws_trades_task(self, symbol: str) -> None:
        cache = self._cache[symbol]

        async def body() -> None:
            assert self._exchange is not None
            trades = await self._exchange.watch_trades(symbol)
            now_bucket = int(time.time() // 60)
            for t in trades or []:
                ts = int(t.get("timestamp") or 0)
                side = (t.get("side") or "").lower()
                try:
                    amount = float(t.get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ts <= 0 or amount <= 0:
                    continue
                if side == "buy":
                    delta = amount
                elif side == "sell":
                    delta = -amount
                else:
                    continue
                cache.trades_ring.append((ts, delta))
                # Bucket aggregation for robust z baseline (60s buckets)
                bucket_key = ts // 60_000
                if cache.cvd_buckets and cache.cvd_buckets[-1][0] == bucket_key:
                    last_key, last_val = cache.cvd_buckets[-1]
                    cache.cvd_buckets[-1] = (last_key, last_val + delta)
                else:
                    cache.cvd_buckets.append((bucket_key, delta))
            cache.touch("trades")

        await self._backoff_loop(f"trades[{symbol}]", body)

    async def _ws_ohlcv_task(self, symbol: str, timeframe: str) -> None:
        cache = self._cache[symbol]
        target = {
            "1m": (cache.ohlcv_1m, "ohlcv_1m"),
            "5m": (cache.ohlcv_5m, "ohlcv_5m"),
            "1h": (cache.ohlcv_1h, "ohlcv_1h"),
        }[timeframe]
        ring, field_name = target

        async def body() -> None:
            assert self._exchange is not None
            ohlcv = await self._exchange.watch_ohlcv(symbol, timeframe)
            if not ohlcv:
                return
            for bar in ohlcv:
                if not bar or len(bar) < 6:
                    continue
                # Replace last bar if same timestamp; else append
                if ring and ring[-1][0] == bar[0]:
                    ring[-1] = list(bar)
                else:
                    ring.append(list(bar))
            # Track swing-low changes for cvd_at_prev_low refresh (5m only)
            if timeframe == "5m" and len(ring) >= 2:
                swing_low, _ = compute_swing_levels(list(ring))
                if cache.last_swing_low_5m_seen is None or swing_low < cache.last_swing_low_5m_seen:
                    cache.last_swing_low_5m_seen = swing_low
                    now_ms = int(time.time() * 1000)
                    cache.cvd_at_prev_low = cvd_in_window(cache.trades_ring, now_ms, WINDOW_15M_MS)
            cache.touch(field_name)

        await self._backoff_loop(f"ohlcv_{timeframe}[{symbol}]", body)

    # ── REST poll tasks ─────────────────────────────────────────────────

    async def _funding_poll_task(self, symbol: str) -> None:
        cache = self._cache[symbol]

        async def body() -> None:
            assert self._exchange is not None
            data = await self._exchange.fetch_funding_rate(symbol)
            rate = data.get("fundingRate")
            settlement = data.get("nextFundingTimestamp") or data.get("fundingTimestamp")
            if rate is not None:
                cache.funding_rate = float(rate)
            if settlement is not None:
                cache.funding_settlement_ms = int(settlement)
            cache.touch("funding")
            await asyncio.sleep(FUNDING_POLL_SEC)

        await self._backoff_loop(f"funding[{symbol}]", body)

    async def _oi_poll_task(self, symbol: str) -> None:
        cache = self._cache[symbol]

        async def body() -> None:
            assert self._exchange is not None
            history = await self._exchange.fetch_open_interest_history(symbol, "1h", 24)
            if history and len(history) >= 2:
                first = float(history[0].get("openInterestValue") or history[0].get("openInterest") or 0)
                last = float(history[-1].get("openInterestValue") or history[-1].get("openInterest") or 0)
                if first > 0:
                    cache.oi_growth_24h_pct = ((last - first) / first) * 100.0
            cache.touch("oi")
            await asyncio.sleep(OI_POLL_SEC)

        await self._backoff_loop(f"oi[{symbol}]", body)

    async def _spot_perp_poll_task(self, symbol: str) -> None:
        cache = self._cache[symbol]
        spot_symbol = symbol.split(":")[0]                       # "ETH/USDT:USDT" → "ETH/USDT"

        async def body() -> None:
            ex = self._spot_exchange or self._exchange
            assert ex is not None
            ticker = await ex.fetch_ticker(spot_symbol)
            last = ticker.get("last") or ticker.get("close")
            if last is not None:
                cache.spot_price = float(last)
            cache.touch("spot")
            await asyncio.sleep(SPOT_POLL_SEC)

        await self._backoff_loop(f"spot[{symbol}]", body)

    async def _btc_correlation_task(self) -> None:
        async def body() -> None:
            assert self._exchange is not None
            try:
                btc_bars = await self._exchange.fetch_ohlcv("BTC/USDT:USDT", "1d", 60)
                eth_bars = await self._exchange.fetch_ohlcv("ETH/USDT:USDT", "1d", 60)
            except Exception as e:
                log.warning("BTC-ETH corr fetch failed: %s", e)
                await asyncio.sleep(BTC_CORR_POLL_SEC)
                return
            if btc_bars and eth_bars:
                btc_closes = [float(b[4]) for b in btc_bars]
                eth_closes = [float(b[4]) for b in eth_bars]
                # Daily returns
                btc_rets = [
                    (btc_closes[i] - btc_closes[i - 1]) / btc_closes[i - 1]
                    for i in range(1, len(btc_closes))
                    if btc_closes[i - 1] > 0
                ]
                eth_rets = [
                    (eth_closes[i] - eth_closes[i - 1]) / eth_closes[i - 1]
                    for i in range(1, len(eth_closes))
                    if eth_closes[i - 1] > 0
                ]
                self._btc_eth_corr_60d = pearson_correlation(btc_rets, eth_rets)
                log.info("btc_eth_corr_60d updated: %.3f", self._btc_eth_corr_60d)
            await asyncio.sleep(BTC_CORR_POLL_SEC)

        await self._backoff_loop("btc_corr", body)

    async def _dxy_task(self) -> None:
        """
        Best-effort DXY intraday %.

        Strategy: poll yfinance 'DX-Y.NYB' (5min cadence). Initial value set as
        session open. If yfinance fails (delisted ticker) — leave 0.0 and log.
        Phase 6.5 will replace with FRED API.
        """
        async def body() -> None:
            value: Optional[float] = None
            try:
                # yfinance is sync; offload to default executor.
                import yfinance as yf                                 # type: ignore[import]
                loop = asyncio.get_running_loop()
                ticker = yf.Ticker("DX-Y.NYB")

                def _fetch() -> Optional[float]:
                    hist = ticker.history(period="1d", interval="5m")
                    if hist.empty:
                        return None
                    return float(hist["Close"].iloc[-1])

                value = await loop.run_in_executor(None, _fetch)
            except Exception as e:
                log.warning("DXY fetch failed (%s) — leaving last value", e)

            if value is not None and value > 0:
                if self._dxy_session_open is None:
                    self._dxy_session_open = value
                self._dxy_intraday_pct = ((value - self._dxy_session_open) / self._dxy_session_open) * 100.0
            await asyncio.sleep(DXY_POLL_SEC)

        await self._backoff_loop("dxy", body)

    async def _bvol_task(self) -> None:
        """Best-effort Deribit DVOL as BVOL proxy."""
        async def body() -> None:
            assert self._http is not None
            try:
                resp = await self._http.get(
                    "https://www.deribit.com/api/v2/public/get_index?index_name=btc_dvol",
                    timeout=10.0,
                )
                # httpx Response has .json()
                data = resp.json() if hasattr(resp, "json") else {}
                if isinstance(data, dict):
                    res = data.get("result") or {}
                    val = res.get("price") or res.get("value")
                    if val is not None:
                        self._bvol_index = float(val)
            except Exception as e:
                log.warning("BVOL (Deribit DVOL) fetch failed: %s", e)
            await asyncio.sleep(BVOL_POLL_SEC)

        await self._backoff_loop("bvol", body)

    # ── Health monitor ──────────────────────────────────────────────────

    async def _health_monitor_task(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                pass
            now_ms = int(time.time() * 1000)
            stale_symbols = []
            for sym, cache in self._cache.items():
                worst = cache.freshness_ms(
                    ["book", "trades", "ohlcv_1m"], now_ms=now_ms
                )
                if worst > WS_HEALTH_DEAD_SEC * 1000:
                    for f in ("book", "trades", "ohlcv_1m"):
                        if (now_ms - cache.last_update_ms.get(f, 0)) > WS_HEALTH_DEAD_SEC * 1000:
                            cache.mark_dead(f)
                    stale_symbols.append(sym)

            if len(stale_symbols) >= DEGRADED_SYMBOLS_THRESHOLD:
                # Throttle alerts: at most once per 10 minutes
                if now_ms - self._last_degraded_alert_ms > 600_000:
                    self._last_degraded_alert_ms = now_ms
                    if self.degraded_alert_callback is not None:
                        try:
                            await self.degraded_alert_callback(stale_symbols)
                        except Exception as e:
                            log.warning("degraded alert callback failed: %s", e)
                    else:
                        log.warning("provider DEGRADED — stale symbols: %s", stale_symbols)

    # ── Warmup ──────────────────────────────────────────────────────────

    async def _wait_warmup(self) -> None:
        """Wait until at least one primary symbol has populated the basics."""
        required = ["book", "trades", "ohlcv_1m", "ohlcv_5m", "ohlcv_1h", "funding", "spot"]
        while not self._stop_event.is_set():
            for sym in self.symbols:
                cache = self._cache[sym]
                if cache.freshness_ms(required) <= self.stale_threshold_sec * 1000:
                    return
            await asyncio.sleep(0.2)
