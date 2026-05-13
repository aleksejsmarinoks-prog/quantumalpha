"""
LV1 — CVD Streaming Calculator
==============================

Streaming Cumulative Volume Delta from ccxt.pro `watch_trades` payloads.

CCXT trade format:
    {"timestamp": ms, "side": "buy"|"sell", "amount": float, "price": float, ...}

CVD = Σ (buy_volume - sell_volume), in base-currency units.

This module:
  - Maintains rolling 15-min CVD value per symbol
  - Exposes 60s rolling window for z-score calculations
  - Tracks rolling 30m median + MAD for robust z (ChatGPT's contribution)
  - Records CVD-at-prev-low for divergence detection

NOT directly responsible for WebSocket connection — caller supplies trade
batches via .ingest_trades(). Provides separation of I/O from logic for
deterministic testing.

Author: QuantForge / QuantumAlpha
Phase: 6.1
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

WINDOW_60S_MS = 60_000
WINDOW_15M_MS = 15 * 60 * 1000
WINDOW_30M_MS = 30 * 60 * 1000


# ─────────────────────────────────────────────────────────────────────────────
# Data containers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CvdSample:
    """Single tick: signed volume delta + timestamp."""
    ts_ms: int
    delta: float                # positive for buys, negative for sells (base-ccy units)
    price: float


@dataclass
class CvdSnapshot:
    """Read-only view returned by CvdStream.snapshot()."""
    symbol: str
    cvd_15m: float
    cvd_60s: float
    rolling_median_30m: float
    rolling_mad_30m: float
    n_samples_30m: int
    last_ts_ms: int


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────

class CvdStream:
    """
    Per-symbol streaming CVD with rolling windows.

    Usage:
        cvd = CvdStream("ETH/USDT:USDT")
        cvd.ingest_trades([
            {"timestamp": 1714000000000, "side": "buy",  "amount": 0.5, "price": 3500.0},
            {"timestamp": 1714000001000, "side": "sell", "amount": 0.3, "price": 3499.5},
        ])
        snap = cvd.snapshot()
    """

    def __init__(
        self,
        symbol: str,
        window_15m_ms: int = WINDOW_15M_MS,
        window_60s_ms: int = WINDOW_60S_MS,
        window_30m_ms: int = WINDOW_30M_MS,
    ):
        self.symbol = symbol
        self._window_15m = window_15m_ms
        self._window_60s = window_60s_ms
        self._window_30m = window_30m_ms
        self._samples: Deque[CvdSample] = deque()
        self._last_ts_ms: int = 0
        # CVD value snapshots for divergence reference (timestamp ms → cvd_15m at that point)
        self._cvd_history: Deque[tuple[int, float]] = deque()
        self._cvd_at_prev_low: float = 0.0

    # ── Ingest ───────────────────────────────────────────────────────────
    def ingest_trades(self, trades: Iterable[dict]) -> None:
        """Process a batch of CCXT trade dicts."""
        for t in trades:
            self._ingest_one(t)
        self._evict_old()

    def _ingest_one(self, trade: dict) -> None:
        ts = int(trade.get("timestamp") or 0)
        if ts <= 0:
            return
        side = (trade.get("side") or "").lower()
        try:
            amount = float(trade.get("amount") or 0.0)
            price = float(trade.get("price") or 0.0)
        except (TypeError, ValueError):
            return
        if amount <= 0 or price <= 0:
            return
        if side == "buy":
            delta = +amount
        elif side == "sell":
            delta = -amount
        else:
            return
        self._samples.append(CvdSample(ts, delta, price))
        self._last_ts_ms = max(self._last_ts_ms, ts)

    def _evict_old(self) -> None:
        """Drop samples older than 30m window."""
        if not self._samples:
            return
        cutoff = self._last_ts_ms - self._window_30m
        while self._samples and self._samples[0].ts_ms < cutoff:
            self._samples.popleft()

    # ── Computations ─────────────────────────────────────────────────────
    def cvd_in_window(self, window_ms: int) -> float:
        """Sum of delta over last `window_ms` from latest tick."""
        if not self._samples:
            return 0.0
        cutoff = self._last_ts_ms - window_ms
        total = 0.0
        # Iterate in reverse for early exit
        for s in reversed(self._samples):
            if s.ts_ms >= cutoff:
                total += s.delta
            else:
                break
        return total

    def cvd_15m(self) -> float:
        return self.cvd_in_window(self._window_15m)

    def cvd_60s(self) -> float:
        return self.cvd_in_window(self._window_60s)

    def rolling_baseline_30m(self, bucket_ms: int = 60_000) -> tuple[float, float, int]:
        """
        Return (median, MAD, n_buckets) of bucketed CVD-deltas over 30m window.
        Buckets default to 60s. Used for robust z-score in lv1_signals.
        """
        if not self._samples:
            return 0.0, 0.0, 0
        cutoff = self._last_ts_ms - self._window_30m
        # Bucket buy/sell deltas by floor(ts/bucket_ms)
        buckets: dict[int, float] = {}
        for s in self._samples:
            if s.ts_ms < cutoff:
                continue
            key = s.ts_ms // bucket_ms
            buckets[key] = buckets.get(key, 0.0) + s.delta
        if not buckets:
            return 0.0, 0.0, 0
        values = list(buckets.values())
        median = statistics.median(values)
        absdev = [abs(v - median) for v in values]
        mad = statistics.median(absdev)
        return median, mad, len(values)

    # ── Public snapshot ──────────────────────────────────────────────────
    def snapshot(self) -> CvdSnapshot:
        median, mad, n = self.rolling_baseline_30m()
        return CvdSnapshot(
            symbol=self.symbol,
            cvd_15m=self.cvd_15m(),
            cvd_60s=self.cvd_60s(),
            rolling_median_30m=median,
            rolling_mad_30m=mad,
            n_samples_30m=n,
            last_ts_ms=self._last_ts_ms,
        )

    # ── Divergence reference ─────────────────────────────────────────────
    def mark_prev_low(self) -> None:
        """Caller invokes when a new swing-low is detected to capture cvd-at-low."""
        self._cvd_at_prev_low = self.cvd_15m()

    @property
    def cvd_at_prev_low(self) -> float:
        return self._cvd_at_prev_low


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: robust z-score
# ─────────────────────────────────────────────────────────────────────────────

def robust_z_score(value: float, median: float, mad: float) -> float:
    """
    Robust z-score using median + MAD.
    Returns 0.0 for degenerate MAD to avoid /0 explosions.
    """
    if mad <= 0:
        return 0.0
    return (value - median) / mad
