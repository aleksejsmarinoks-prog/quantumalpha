"""
Test fixtures for QA Backtester Phase 6.3.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from bot.backtester.data_loader import BybitDataLoader
from bot.backtester.models import Fill, OrderType, Side


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic data generators
# ─────────────────────────────────────────────────────────────────────────────

def make_klines(
    start: datetime,
    n_bars: int,
    bar_minutes: int = 5,
    base_price: float = 3500.0,
    trend: float = 0.0,
    volatility: float = 0.005,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Synthetic OHLCV with controllable trend/vol. Returns DataFrame with
    UTC datetime index, columns: [open, high, low, close, volume].
    """
    rng = np.random.default_rng(seed)
    timestamps = [start + timedelta(minutes=i * bar_minutes) for i in range(n_bars)]
    prices = [base_price]
    for i in range(1, n_bars):
        drift = trend / n_bars
        shock = rng.normal(0, volatility)
        prices.append(prices[-1] * (1.0 + drift + shock))
    rows = []
    for ts, mid in zip(timestamps, prices):
        spread = mid * volatility * 0.5
        o = mid * (1.0 + rng.normal(0, volatility * 0.3))
        c = mid * (1.0 + rng.normal(0, volatility * 0.3))
        h = max(o, c) + abs(spread)
        l = min(o, c) - abs(spread)
        v = abs(rng.normal(1000.0, 200.0))
        rows.append([o, h, l, c, v])
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex(timestamps, tz="UTC"))
    return df


def make_funding_history(
    start: datetime,
    n_settlements: int,
    base_rate: float = 0.0001,
    seed: int = 42,
) -> pd.DataFrame:
    """8h funding settlements with mean-zero noise."""
    rng = np.random.default_rng(seed)
    timestamps = [start + timedelta(hours=8 * i) for i in range(n_settlements)]
    rates = [base_rate + rng.normal(0, 0.0002) for _ in range(n_settlements)]
    df = pd.DataFrame({"funding_rate": rates}, index=pd.DatetimeIndex(timestamps, tz="UTC"))
    return df


def make_mean_reverting_series(
    start: datetime, n_bars: int, base: float = 3500.0, amplitude: float = 50.0,
    cycle_bars: int = 24, bar_minutes: int = 5,
) -> pd.DataFrame:
    """Sinusoidal price series — clean reversion test data."""
    timestamps = [start + timedelta(minutes=i * bar_minutes) for i in range(n_bars)]
    prices = [base + amplitude * np.sin(2 * np.pi * i / cycle_bars) for i in range(n_bars)]
    rows = []
    for p in prices:
        rows.append([p, p * 1.001, p * 0.999, p, 1000.0])
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                        index=pd.DatetimeIndex(timestamps, tz="UTC"))


# ─────────────────────────────────────────────────────────────────────────────
# HTTP mock for data_loader tests
# ─────────────────────────────────────────────────────────────────────────────

class FakeBybitResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeBybitHttp:
    """
    Stand-in for httpx.Client.get. Tests pre-load `kline_responses` and
    `funding_responses` as lists of dicts.
    """

    def __init__(self):
        self.kline_responses: list[list] = []     # list of (rows) lists, each is a chunk
        self.funding_responses: list[list] = []
        self.calls: list[tuple[str, dict]] = []
        self.raise_on_call: Optional[int] = None
        self.call_count: int = 0

    def queue_kline_chunk(self, rows: list[list]) -> None:
        self.kline_responses.append(rows)

    def queue_funding_chunk(self, rows: list[dict]) -> None:
        self.funding_responses.append(rows)

    def get(self, url: str, params: Optional[dict] = None, timeout: float = 15.0) -> FakeBybitResponse:
        self.call_count += 1
        self.calls.append((url, params or {}))
        if self.raise_on_call is not None and self.call_count == self.raise_on_call:
            raise RuntimeError("forced HTTP error")
        if "kline" in url:
            payload = self.kline_responses.pop(0) if self.kline_responses else []
            # Bybit returns newest first
            payload = list(reversed(payload))
            return FakeBybitResponse({"retCode": 0, "result": {"list": payload}})
        if "funding/history" in url:
            payload = self.funding_responses.pop(0) if self.funding_responses else []
            return FakeBybitResponse({"retCode": 0, "result": {"list": payload}})
        return FakeBybitResponse({"retCode": 0, "result": {}})


# ─────────────────────────────────────────────────────────────────────────────
# Fill builders
# ─────────────────────────────────────────────────────────────────────────────

def make_fill(
    timestamp: Optional[datetime] = None,
    symbol: str = "ETHUSDT",
    side: Side = Side.BUY,
    size_usd: float = 200.0,
    fill_price: float = 3500.0,
    fee_usd: float = 0.04,
    slippage_bp: float = 1.0,
    order_type: OrderType = OrderType.MAKER,
) -> Fill:
    if timestamp is None:
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Fill(timestamp, symbol, side, size_usd, fill_price, fee_usd, slippage_bp, order_type)


# ─────────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_cache_dir(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture
def fake_http() -> FakeBybitHttp:
    return FakeBybitHttp()


@pytest.fixture
def loader(tmp_cache_dir, fake_http) -> BybitDataLoader:
    return BybitDataLoader(
        cache_root=tmp_cache_dir,
        http_client=fake_http,
        sleep_fn=lambda _s: None,             # zero sleep in tests
    )


@pytest.fixture
def sample_klines() -> pd.DataFrame:
    return make_klines(datetime(2026, 1, 1, tzinfo=timezone.utc), n_bars=288, bar_minutes=5)


@pytest.fixture
def sample_funding() -> pd.DataFrame:
    return make_funding_history(datetime(2026, 1, 1, tzinfo=timezone.utc), n_settlements=3)
