"""
QA Backtester — Historical Data Loader
========================================

Fetches Bybit v5 historical klines and funding rate history. Caches as
gzipped CSV in `data/backtest_cache/{symbol}/{timeframe}/` for fast
re-runs.

Bybit v5 endpoints used:
  - /v5/market/kline?category=linear&symbol=ETHUSDT&interval=5
      → list of [start_ms, open, high, low, close, volume, turnover]
  - /v5/market/funding/history?category=linear&symbol=ETHUSDT
      → list of {symbol, fundingRate, fundingRateTimestamp}

Rate limit: ~120 req/min. Sleep 0.6s between requests. Resume on transient
network errors with exponential backoff.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

import pandas as pd


log = logging.getLogger("qa.backtester.data_loader")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CACHE_ROOT = Path("data/backtest_cache")
BYBIT_KLINE_LIMIT = 1000                # max records per Bybit kline request
BYBIT_FUNDING_LIMIT = 200
REQ_SLEEP_SEC = 0.6                     # under 120 req/min
MAX_RETRIES = 3

TIMEFRAME_MS = {
    "1": 60_000,                        # Bybit "interval" values
    "5": 5 * 60_000,
    "15": 15 * 60_000,
    "60": 3600_000,
    "1m": 60_000,                       # CCXT-style aliases
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 3600_000,
}

# Bybit interval mapping for v5 API (numeric strings)
TF_TO_BYBIT = {"1m": "1", "5m": "5", "15m": "15", "1h": "60"}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP transport Protocol (for injection in tests)
# ─────────────────────────────────────────────────────────────────────────────

class HttpClientProto(Protocol):
    """Minimal interface for HTTP GET — covers httpx and any mock."""

    def get(self, url: str, params: Optional[dict] = None, timeout: float = 15.0) -> "HttpResponseProto": ...


class HttpResponseProto(Protocol):
    status_code: int

    def json(self) -> dict: ...


# ─────────────────────────────────────────────────────────────────────────────
# BybitDataLoader
# ─────────────────────────────────────────────────────────────────────────────

class BybitDataLoader:
    """
    Loads Bybit v5 historical klines & funding rate history.
    Caches as gzipped CSV.

    Usage:
        loader = BybitDataLoader()
        df_kl = loader.fetch_klines("ETHUSDT", "5m", start, end)
        df_fr = loader.fetch_funding_history("ETHUSDT", start, end)
    """

    BASE_URL = "https://api.bybit.com"

    def __init__(
        self,
        cache_root: Path = DEFAULT_CACHE_ROOT,
        http_client: Optional[HttpClientProto] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        category: str = "linear",
    ):
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._http = http_client
        self._sleep = sleep_fn
        self.category = category

    # ── Cache paths ──────────────────────────────────────────────────────
    def _kline_cache_path(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
        d = self.cache_root / symbol / timeframe
        d.mkdir(parents=True, exist_ok=True)
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        return d / f"klines_{s}_{e}.csv.gz"

    def _funding_cache_path(self, symbol: str, start: datetime, end: datetime) -> Path:
        d = self.cache_root / symbol / "funding"
        d.mkdir(parents=True, exist_ok=True)
        s = start.strftime("%Y%m%d")
        e = end.strftime("%Y%m%d")
        return d / f"funding_{s}_{e}.csv.gz"

    # ── HTTP helpers ─────────────────────────────────────────────────────
    def _get_http(self) -> HttpClientProto:
        if self._http is None:
            try:
                import httpx                                     # type: ignore[import]
            except ImportError as e:
                raise RuntimeError("httpx required for default HTTP transport") from e
            self._http = httpx.Client(timeout=15.0)              # type: ignore[assignment]
        return self._http                                         # type: ignore[return-value]

    def _request(self, path: str, params: dict) -> dict:
        """GET with retries + sleep. Returns parsed JSON 'result' dict."""
        url = f"{self.BASE_URL}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                http = self._get_http()
                resp = http.get(url, params=params, timeout=15.0)
                if getattr(resp, "status_code", 200) != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} from {path}")
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise RuntimeError(f"unexpected payload type {type(payload)}")
                if payload.get("retCode") not in (0, None):
                    raise RuntimeError(f"Bybit retCode={payload.get('retCode')} msg={payload.get('retMsg')}")
                return payload.get("result") or {}
            except Exception as e:
                last_err = e
                backoff = 2 ** attempt
                log.warning("request fail attempt %d/%d (%s) — backoff %ds", attempt + 1, MAX_RETRIES, e, backoff)
                self._sleep(backoff)
        assert last_err is not None
        raise last_err

    # ── Public: klines ───────────────────────────────────────────────────
    def fetch_klines(
        self,
        symbol: str,
        timeframe: str,                 # "1m", "5m", "15m", "1h" or Bybit numeric strings
        start: datetime,
        end: datetime,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Returns DataFrame indexed by UTC datetime with columns:
            [open, high, low, close, volume].
        """
        cache_path = self._kline_cache_path(symbol, timeframe, start, end)
        if cache_path.exists() and not force_refresh:
            log.info("cache HIT %s", cache_path.name)
            return self._read_cached_csv(cache_path)

        log.info("fetching klines %s %s %s..%s", symbol, timeframe, start.date(), end.date())
        df = self._fetch_klines_chunked(symbol, timeframe, start, end)
        self._write_cached_csv(df, cache_path)
        return df

    def _fetch_klines_chunked(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        bybit_interval = TF_TO_BYBIT.get(timeframe, timeframe)
        tf_ms_key = bybit_interval if bybit_interval in TIMEFRAME_MS else timeframe
        bar_ms = TIMEFRAME_MS.get(tf_ms_key, 60_000)

        start_ms = int(start.replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(end.replace(tzinfo=timezone.utc).timestamp() * 1000)

        all_rows: list[list] = []
        cursor = start_ms
        while cursor < end_ms:
            chunk_end = min(cursor + BYBIT_KLINE_LIMIT * bar_ms, end_ms)
            result = self._request("/v5/market/kline", {
                "category": self.category,
                "symbol": symbol,
                "interval": bybit_interval,
                "start": cursor,
                "end": chunk_end,
                "limit": BYBIT_KLINE_LIMIT,
            })
            rows = result.get("list") or []
            # Bybit returns newest first — invert
            rows = list(reversed(rows))
            if not rows:
                cursor = chunk_end
            else:
                all_rows.extend(rows)
                last_ts = int(rows[-1][0])
                cursor = last_ts + bar_ms
            self._sleep(REQ_SLEEP_SEC)

        if not all_rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
        df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col])
        df = df.drop(columns=["turnover"]).set_index("ts").sort_index()
        # Dedup
        df = df[~df.index.duplicated(keep="first")]
        return df

    # ── Public: funding history ──────────────────────────────────────────
    def fetch_funding_history(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Returns DataFrame indexed by UTC datetime with single column [funding_rate].
        Bybit funds every 8h.
        """
        cache_path = self._funding_cache_path(symbol, start, end)
        if cache_path.exists() and not force_refresh:
            log.info("funding cache HIT %s", cache_path.name)
            return self._read_cached_csv(cache_path)

        start_ms = int(start.replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(end.replace(tzinfo=timezone.utc).timestamp() * 1000)
        all_rows: list[dict] = []
        cursor_end = end_ms
        while cursor_end > start_ms:
            result = self._request("/v5/market/funding/history", {
                "category": self.category,
                "symbol": symbol,
                "startTime": start_ms,
                "endTime": cursor_end,
                "limit": BYBIT_FUNDING_LIMIT,
            })
            rows = result.get("list") or []
            if not rows:
                break
            all_rows.extend(rows)
            oldest_ts = int(rows[-1]["fundingRateTimestamp"])
            if oldest_ts <= start_ms:
                break
            cursor_end = oldest_ts - 1
            self._sleep(REQ_SLEEP_SEC)

        if not all_rows:
            return pd.DataFrame(columns=["funding_rate"])

        df = pd.DataFrame(all_rows)
        df["ts"] = pd.to_datetime(df["fundingRateTimestamp"].astype("int64"), unit="ms", utc=True)
        df["funding_rate"] = pd.to_numeric(df["fundingRate"])
        df = df[["ts", "funding_rate"]].set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="first")]
        self._write_cached_csv(df, cache_path)
        return df

    # ── Cache I/O ────────────────────────────────────────────────────────
    def _read_cached_csv(self, path: Path) -> pd.DataFrame:
        with gzip.open(path, "rt") as f:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
        # Ensure UTC tz
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        return df

    def _write_cached_csv(self, df: pd.DataFrame, path: Path) -> None:
        if df.empty:
            return
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as f:
            df.to_csv(f)
        os.replace(tmp, path)
        log.info("cached %d rows → %s", len(df), path.name)

    # ── Convenience: ADV (avg daily volume) ──────────────────────────────
    def estimate_adv_24h_usd(self, df_klines: pd.DataFrame) -> float:
        """Rough USD ADV from a kline DataFrame (close*volume avg over last 24h)."""
        if df_klines.empty:
            return 0.0
        notional = (df_klines["close"] * df_klines["volume"])
        last_24h = notional.tail(int(86400 / 60 / 5))            # assume 5m bars
        return float(last_24h.sum()) if not last_24h.empty else float(notional.sum())
