"""Test fixtures for Phase 6.2."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Optional

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# FakeExchange — mock ccxt.pro
# ─────────────────────────────────────────────────────────────────────────────

class FakeExchange:
    """
    Stand-in for ccxt.pro Exchange. Tests preload `payload_queues` per (symbol,
    method) — each watch_*() call dequeues one payload. When queue empty,
    awaits indefinitely (simulates idle WS) so background tasks can be cancelled
    cleanly.

    Set `raise_on` to a method name to make that method raise once (then succeed
    on the second call) — useful for testing reconnect/backoff.
    """

    def __init__(self) -> None:
        self.book_queues: dict[str, deque] = {}
        self.trades_queues: dict[str, deque] = {}
        self.ohlcv_queues: dict[tuple[str, str], deque] = {}
        self.funding_queues: dict[str, deque] = {}
        self.oi_queues: dict[str, deque] = {}
        self.ticker_queues: dict[str, deque] = {}
        self.daily_ohlcv: dict[str, list[list]] = {}
        self.calls: list[str] = []
        self.raise_on: Optional[str] = None
        self._raised: set[str] = set()
        self.closed = False
        # When True, watch_* awaits until cancelled instead of looping fast
        self.idle_when_empty: bool = True

    def queue_book(self, symbol: str, book: dict) -> None:
        self.book_queues.setdefault(symbol, deque()).append(book)

    def queue_trades(self, symbol: str, trades: list[dict]) -> None:
        self.trades_queues.setdefault(symbol, deque()).append(trades)

    def queue_ohlcv(self, symbol: str, timeframe: str, bars: list[list]) -> None:
        self.ohlcv_queues.setdefault((symbol, timeframe), deque()).append(bars)

    def queue_funding(self, symbol: str, payload: dict) -> None:
        self.funding_queues.setdefault(symbol, deque()).append(payload)

    def queue_oi(self, symbol: str, history: list[dict]) -> None:
        self.oi_queues.setdefault(symbol, deque()).append(history)

    def queue_ticker(self, symbol: str, ticker: dict) -> None:
        self.ticker_queues.setdefault(symbol, deque()).append(ticker)

    def set_daily_ohlcv(self, symbol: str, bars: list[list]) -> None:
        self.daily_ohlcv[symbol] = bars

    async def _maybe_raise(self, name: str) -> None:
        if self.raise_on == name and name not in self._raised:
            self._raised.add(name)
            raise RuntimeError(f"forced error in {name}")

    async def _drain_or_idle(self, q: Optional[deque]) -> Any:
        if q and len(q) > 0:
            return q.popleft()
        # Idle until cancelled
        while True:
            await asyncio.sleep(3600)

    async def watch_order_book(self, symbol: str, limit: int = 50) -> dict:
        self.calls.append(f"watch_order_book:{symbol}")
        await self._maybe_raise("watch_order_book")
        return await self._drain_or_idle(self.book_queues.get(symbol))

    async def watch_trades(self, symbol: str) -> list[dict]:
        self.calls.append(f"watch_trades:{symbol}")
        await self._maybe_raise("watch_trades")
        return await self._drain_or_idle(self.trades_queues.get(symbol))

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> list[list]:
        self.calls.append(f"watch_ohlcv:{symbol}:{timeframe}")
        await self._maybe_raise("watch_ohlcv")
        return await self._drain_or_idle(self.ohlcv_queues.get((symbol, timeframe)))

    async def fetch_funding_rate(self, symbol: str) -> dict:
        self.calls.append(f"fetch_funding_rate:{symbol}")
        q = self.funding_queues.get(symbol)
        if q and len(q) > 0:
            return q.popleft()
        # Default
        return {"fundingRate": 0.0001, "nextFundingTimestamp": int(time.time() * 1000) + 8 * 3600 * 1000}

    async def fetch_open_interest_history(self, symbol: str, timeframe: str = "1h", limit: int = 24) -> list[dict]:
        self.calls.append(f"fetch_oi:{symbol}")
        q = self.oi_queues.get(symbol)
        if q and len(q) > 0:
            return q.popleft()
        return [{"openInterestValue": 1_000_000.0}, {"openInterestValue": 1_050_000.0}]

    async def fetch_ticker(self, symbol: str) -> dict:
        self.calls.append(f"fetch_ticker:{symbol}")
        q = self.ticker_queues.get(symbol)
        if q and len(q) > 0:
            return q.popleft()
        return {"last": 3500.0}

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> list[list]:
        self.calls.append(f"fetch_ohlcv:{symbol}:{timeframe}")
        return list(self.daily_ohlcv.get(symbol, []))

    async def close(self) -> None:
        self.closed = True


# ─────────────────────────────────────────────────────────────────────────────
# FakeHttp — for Deribit DVOL polling
# ─────────────────────────────────────────────────────────────────────────────

class FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    def __init__(self, default_dvol: float = 65.0) -> None:
        self.calls: list[str] = []
        self.default_dvol = default_dvol
        self.next_response: Optional[Any] = None
        self.raise_next: bool = False
        self.closed: bool = False

    async def get(self, url: str, *, timeout: float = 10.0) -> Any:
        self.calls.append(url)
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("forced http error")
        if self.next_response is not None:
            r, self.next_response = self.next_response, None
            return r
        return FakeHttpResponse({"result": {"price": self.default_dvol}})

    async def aclose(self) -> None:
        self.closed = True


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build canonical OHLCV bars
# ─────────────────────────────────────────────────────────────────────────────

def make_ohlcv(n: int, base_price: float = 3500.0, ts_ms_start: Optional[int] = None,
               step_ms: int = 60_000) -> list[list]:
    """Return n synthetic bars with oscillating high/low for ATR != 0."""
    if ts_ms_start is None:
        ts_ms_start = int(time.time() * 1000) - n * step_ms
    bars = []
    for i in range(n):
        ts = ts_ms_start + i * step_ms
        o = base_price + i * 0.5
        c = o + (1.0 if i % 2 == 0 else -1.0)
        h = max(o, c) + 2.0
        l = min(o, c) - 2.0
        v = 100.0
        bars.append([ts, o, h, l, c, v])
    return bars


def make_book(mid: float = 3500.0, spread_bps_: float = 4.0, depth_levels: int = 5,
              size_per_level: float = 5.0) -> dict:
    half = mid * (spread_bps_ / 2.0 / 10_000.0)
    bid = mid - half
    ask = mid + half
    bids = [[bid - i * (mid * 0.0001), size_per_level] for i in range(depth_levels)]
    asks = [[ask + i * (mid * 0.0001), size_per_level] for i in range(depth_levels)]
    return {"bids": bids, "asks": asks, "timestamp": int(time.time() * 1000)}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()
