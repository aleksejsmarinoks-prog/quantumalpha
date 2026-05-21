"""
QA Backtest — IndicatorsProvider (Phase 6.3.1a Step 5a)
=========================================================

Precomputed market indicators for backtest replay. Plugs into the
ReplayEngineV2 `indicators_provider` hook from Step 1:

    provider = IndicatorsProvider(bars=all_5m_bars)
    eng = ReplayEngineV2(
        symbol="ETHUSDT",
        indicators_provider=lambda ts, bar: provider.indicators_at(ts),
    )

The adapter then reads from `snapshot.indicators` dict:
    rsi = snapshot.indicators.get("rsi_14_1h")

Anti-lookahead invariant (verified by 4 dedicated tests):
    indicators_at(t) uses ONLY bars whose timestamp is STRICTLY < t.
    Same convention as Step 1 ReplayEngine and Step 2 RegimeDetector.

Provided indicators
-------------------
    rsi_14_1h     RSI 14 computed on 1-hour resampled bars
    returns_1h    Pct change over last completed 1-hour bar
    last_price    Close of most recent strictly-before bar

Resample convention
-------------------
5m bars are aggregated into 1h bars by floor of timestamp to the nearest
hour (UTC). A 1h bar covering 9:00:00-10:00:00 has timestamp 10:00:00 (end of bar).

At 5m bar timestamp t, the 1h bar at timestamp T is visible iff T < t.

Author: QuantumAlpha
Phase: 6.3.1a Step 5a
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .models import Bar

logger = logging.getLogger("qa.backtest.indicators")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndicatorsConfig:
    """Config for indicator computation. Defaults tuned for mean_reversion."""
    rsi_period: int = 14         # Wilder RSI period (on 1h resampled bars)
    resample_minutes: int = 60   # Resample window: 60min = 1h
    close_1h_history_size: int = 20    # Phase 6.3.1b — production needs list of last N 1h closes

    def __post_init__(self) -> None:
        if self.rsi_period < 2:
            raise ValueError("rsi_period must be >= 2")
        if self.resample_minutes <= 0:
            raise ValueError("resample_minutes must be > 0")
        if self.close_1h_history_size < 1:
            raise ValueError("close_1h_history_size must be >= 1")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class IndicatorsProvider:
    """Precomputed market indicators.

    Constructor takes the FULL 5m bar history once. indicators_at(t) returns
    a dict computed from bars strictly before t. Caching: each (t -> dict)
    is memoized on first compute.
    """

    def __init__(
        self,
        bars: Sequence[Bar],
        config: Optional[IndicatorsConfig] = None,
    ):
        if not bars:
            raise ValueError("IndicatorsProvider requires at least 1 bar")
        self.config = config or IndicatorsConfig()
        self._bars: List[Bar] = sorted(bars, key=lambda b: b.timestamp)
        self._bar_timestamps: List[datetime] = [b.timestamp for b in self._bars]

        # Precompute 1h resampled bars
        self._h1_bars: List[Bar] = self._resample_to_1h(self._bars)
        self._h1_timestamps: List[datetime] = [b.timestamp for b in self._h1_bars]

        # Cache for indicators_at results
        self._cache: Dict[datetime, Dict[str, Any]] = {}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def indicators_at(self, timestamp: datetime) -> Dict[str, Any]:
        """Return indicator dict using bars STRICTLY before `timestamp`.

        Missing indicators (insufficient history) are absent from the dict —
        adapters use `dict.get("rsi_14_1h")` returning None to detect warmup.
        """
        if timestamp in self._cache:
            return self._cache[timestamp]

        out: Dict[str, Any] = {}

        # 5m bars strictly before timestamp
        idx_5m = bisect.bisect_left(self._bar_timestamps, timestamp)
        if idx_5m == 0:
            self._cache[timestamp] = out
            return out

        # 1h bars strictly before timestamp
        idx_1h = bisect.bisect_left(self._h1_timestamps, timestamp)
        if idx_1h == 0:
            # No complete 1h bar yet → only last_price available from 5m
            out["last_price"] = self._bars[idx_5m - 1].close
            self._cache[timestamp] = out
            return out

        # last_price = close of most recent strictly-before 5m bar
        out["last_price"] = self._bars[idx_5m - 1].close

        # returns_1h = pct change of last 1h bar's close vs prior 1h bar's close
        if idx_1h >= 2:
            curr_h1 = self._h1_bars[idx_1h - 1]
            prev_h1 = self._h1_bars[idx_1h - 2]
            if prev_h1.close > 0:
                out["returns_1h"] = (curr_h1.close - prev_h1.close) / prev_h1.close

        # rsi_14_1h
        rsi = self._compute_rsi(idx_1h - 1)
        if rsi is not None:
            out["rsi_14_1h"] = rsi

        # close_1h_history (Phase 6.3.1b — production strategies need list of recent 1h closes)
        history_size = self.config.close_1h_history_size
        history_start = max(0, idx_1h - history_size)
        out["close_1h_history"] = [b.close for b in self._h1_bars[history_start:idx_1h]]

        self._cache[timestamp] = out
        return out

    def callable_for_engine(self):
        """Return callable suitable for ReplayEngineV2.indicators_provider."""
        return lambda ts, bar: self.indicators_at(ts)

    # -----------------------------------------------------------------------
    # 5m → 1h resampling
    # -----------------------------------------------------------------------

    def _resample_to_1h(self, bars_5m: Sequence[Bar]) -> List[Bar]:
        """Aggregate 5m bars into 1h bars by floor-to-hour grouping.

        A 1h bar with timestamp T covers 5m bars in (T-1h, T]. The 1h bar
        is emitted only if there's at least one 5m bar within the window.
        """
        if not bars_5m:
            return []

        h1_step = timedelta(minutes=self.config.resample_minutes)
        h1_bars: List[Bar] = []
        current_window_end: Optional[datetime] = None
        window_bars: List[Bar] = []

        for bar in bars_5m:
            # Floor bar.timestamp to nearest hour boundary (ceiling)
            ts = bar.timestamp
            # Round UP to nearest hour boundary (this is the WINDOW END)
            floor_h = ts.replace(minute=0, second=0, microsecond=0)
            if ts > floor_h:
                window_end = floor_h + h1_step
            else:
                window_end = floor_h    # bar.timestamp exactly on hour → ends THIS window

            if current_window_end is None:
                current_window_end = window_end
                window_bars = [bar]
            elif window_end == current_window_end:
                window_bars.append(bar)
            else:
                # Window changed — emit the previous window's 1h bar
                if window_bars:
                    h1_bars.append(self._aggregate_window(window_bars, current_window_end))
                current_window_end = window_end
                window_bars = [bar]

        if window_bars and current_window_end is not None:
            h1_bars.append(self._aggregate_window(window_bars, current_window_end))

        return h1_bars

    @staticmethod
    def _aggregate_window(window_bars: List[Bar], window_end: datetime) -> Bar:
        """Combine multiple 5m bars into a single 1h bar."""
        return Bar(
            timestamp=window_end,
            open=window_bars[0].open,
            high=max(b.high for b in window_bars),
            low=min(b.low for b in window_bars),
            close=window_bars[-1].close,
            volume=sum(b.volume for b in window_bars),
        )

    # -----------------------------------------------------------------------
    # RSI (Wilder smoothing)
    # -----------------------------------------------------------------------

    def _compute_rsi(self, last_h1_idx: int) -> Optional[float]:
        """RSI computed on 1h bars [0 ... last_h1_idx] inclusive.

        Returns None if not enough history (< rsi_period + 1 bars).
        """
        period = self.config.rsi_period
        if last_h1_idx + 1 < period + 1:
            return None

        closes = [b.close for b in self._h1_bars[:last_h1_idx + 1]]

        # Compute deltas
        gains: List[float] = []
        losses: List[float] = []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            gains.append(max(0.0, change))
            losses.append(max(0.0, -change))

        # First avg gain/loss: simple mean of first `period` values
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Wilder smoothing for remaining
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0 and avg_gain == 0:
            return None      # undefined — no price movement at all
        if avg_loss == 0:
            return 100.0     # all gains, no losses → saturated up
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
