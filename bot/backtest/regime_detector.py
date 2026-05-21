"""
QA Backtest — Regime Detector (Phase 6.3.1a Step 2)
=====================================================

Volatility regime detector. Plugs into ReplayEngineV2.regime_provider:

    detector = RegimeDetector(bars=all_bars)
    eng = ReplayEngineV2(
        symbol="ETHUSDT",
        regime_provider=lambda ts: detector.detect_at(ts),
    )

Anti-lookahead invariant (CRITICAL — enforced by 4 dedicated tests):
    detect_at(t) uses ONLY bars whose timestamp is STRICTLY < t.
    The bar AT time t itself is invisible — the detector must work as if
    we don't yet know the current bar's outcome at decision time.

Regime labels:
    LOW_VOL    — ATR / close ratio < low_threshold_pct (default 0.5%)
    NORMAL     — between low and high
    HIGH_VOL   — ATR / close ratio > high_threshold_pct (default 1.5%)
    None       — insufficient history (< atr_period + 1 bars before t)

Default thresholds tuned for crypto perp 5m bars (ETH/USDT, SOL/USDT).
Different timeframes / asset classes may want different bands — pass
`config` to override.

Author: QuantumAlpha
Phase: 6.3.1a Step 2
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .models import Bar

logger = logging.getLogger("qa.backtest.regime")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegimeConfig:
    """Vol + trend bucketing config.

    Volatility regime (default `detect_at()` and `make_regime_provider()`):
      ATR/close ratio percent → LOW_VOL / NORMAL / HIGH_VOL.

    Trend regime (Phase 6.3.1b-B Q6.4 — production contract):
      200×1h MA slope over 24h lookback → BULLISH / NEUTRAL / BEARISH.
      Use `detect_trend_regime()` / `make_trend_regime_provider()`.
    """
    # Volatility (original Step 2 fields)
    atr_period: int = 14               # standard Wilder ATR period
    low_threshold_pct: float = 0.5     # < 0.5% ATR/spot → LOW_VOL
    high_threshold_pct: float = 1.5    # > 1.5% ATR/spot → HIGH_VOL

    # Trend (Phase 6.3.1b-B addition)
    trend_ma_period_h1: int = 200          # 200 × 1h bars (~8.3 days) MA
    trend_slope_lookback_h1: int = 24      # 24 × 1h bars (~1 day) lookback
    bullish_slope_threshold: float = 0.001    # +0.1% MA slope → BULLISH
    bearish_slope_threshold: float = -0.001   # -0.1% MA slope → BEARISH

    def __post_init__(self) -> None:
        if self.atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        if self.low_threshold_pct < 0 or self.high_threshold_pct < 0:
            raise ValueError("thresholds must be >= 0")
        if self.low_threshold_pct >= self.high_threshold_pct:
            raise ValueError(
                f"low_threshold_pct ({self.low_threshold_pct}) must be < "
                f"high_threshold_pct ({self.high_threshold_pct})"
            )
        if self.trend_ma_period_h1 < 2:
            raise ValueError("trend_ma_period_h1 must be >= 2")
        if self.trend_slope_lookback_h1 < 1:
            raise ValueError("trend_slope_lookback_h1 must be >= 1")
        if self.bullish_slope_threshold <= self.bearish_slope_threshold:
            raise ValueError(
                f"bullish_slope_threshold ({self.bullish_slope_threshold}) "
                f"must be > bearish_slope_threshold ({self.bearish_slope_threshold})"
            )


REGIME_LOW = "LOW_VOL"
REGIME_NORMAL = "NORMAL"
REGIME_HIGH = "HIGH_VOL"

# Trend regime strings (production contract — matches base_strategy.py
# apply_risk_gates(regime == "BEARISH") check)
TREND_BULLISH = "BULLISH"
TREND_NEUTRAL = "NEUTRAL"
TREND_BEARISH = "BEARISH"


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class RegimeDetector:
    """Volatility regime detector with strict anti-lookahead.

    Constructor takes the FULL bar history once. detect_at(t) returns the
    regime label computed from bars strictly before t. Bars are stored
    sorted by timestamp; lookups use binary search → O(log N) per query.

    Caching: regime values are memoized per timestamp on first compute.
    """

    def __init__(
        self,
        bars: Sequence[Bar],
        config: Optional[RegimeConfig] = None,
    ):
        if not bars:
            raise ValueError("RegimeDetector requires at least 1 bar; got empty list")
        self.config = config or RegimeConfig()
        # Sort by timestamp (defensive — engine guarantees monotonic but
        # detector may be constructed from arbitrary historical data sources)
        self._bars: List[Bar] = sorted(bars, key=lambda b: b.timestamp)
        self._timestamps: List[datetime] = [b.timestamp for b in self._bars]
        self._cache: Dict[datetime, Optional[str]] = {}

        # Precompute True Range series (one per bar). TR[0] = high - low.
        self._tr: List[float] = self._compute_true_range(self._bars)

        # Phase 6.3.1b-B Q6.4 — precompute 1h-resampled bars for trend detection
        self._h1_bars: List[Bar] = self._resample_to_1h(self._bars)
        self._h1_timestamps: List[datetime] = [b.timestamp for b in self._h1_bars]
        self._trend_cache: Dict[datetime, str] = {}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def detect_at(self, timestamp: datetime) -> Optional[str]:
        """Return regime label using bars STRICTLY before `timestamp`.

        Returns None if insufficient history (need at least atr_period+1 bars).
        """
        if timestamp in self._cache:
            return self._cache[timestamp]

        # Find index of the first bar with timestamp >= `timestamp`
        # Bars STRICTLY before are indices [0, idx-1]
        idx = bisect.bisect_left(self._timestamps, timestamp)
        # Number of bars available before timestamp
        n_available = idx

        if n_available < self.config.atr_period + 1:
            self._cache[timestamp] = None
            return None

        atr = self._atr_at(idx - 1)   # ATR computed using bars [0 ... idx-1]
        if atr is None:
            self._cache[timestamp] = None
            return None

        # Use last available close as the spot reference (still strictly before t)
        last_close = self._bars[idx - 1].close
        if last_close <= 0:
            self._cache[timestamp] = None
            return None

        ratio_pct = (atr / last_close) * 100.0
        regime = self._bucket(ratio_pct)
        self._cache[timestamp] = regime
        return regime

    def get_atr_at(self, timestamp: datetime) -> Optional[float]:
        """Helper for callers that want the raw ATR value (not the bucket)."""
        idx = bisect.bisect_left(self._timestamps, timestamp)
        if idx < self.config.atr_period + 1:
            return None
        return self._atr_at(idx - 1)

    def get_ratio_pct_at(self, timestamp: datetime) -> Optional[float]:
        """Helper: ATR/close ratio (%) at timestamp, computed anti-lookahead."""
        idx = bisect.bisect_left(self._timestamps, timestamp)
        if idx < self.config.atr_period + 1:
            return None
        atr = self._atr_at(idx - 1)
        if atr is None:
            return None
        close = self._bars[idx - 1].close
        if close <= 0:
            return None
        return (atr / close) * 100.0

    # -----------------------------------------------------------------------
    # ATR computation (Wilder smoothing)
    # -----------------------------------------------------------------------

    @staticmethod
    def _compute_true_range(bars: Sequence[Bar]) -> List[float]:
        """True Range per bar. TR[0] = high[0] - low[0] (no prev close)."""
        tr: List[float] = []
        for i, bar in enumerate(bars):
            if i == 0:
                tr.append(bar.high - bar.low)
            else:
                prev_close = bars[i - 1].close
                tr.append(max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                ))
        return tr

    def _atr_at(self, idx: int) -> Optional[float]:
        """Wilder ATR ending at bar `idx` (inclusive). Returns None if not
        enough bars. Uses simple-average seed then Wilder smoothing.
        """
        period = self.config.atr_period
        # Need at least `period` TR values to compute the first ATR
        if idx + 1 < period:
            return None
        # Seed: simple average of first `period` TR values
        seed = sum(self._tr[0:period]) / period
        if idx == period - 1:
            return seed
        # Wilder smoothing: ATR_t = (ATR_{t-1} * (period-1) + TR_t) / period
        atr = seed
        for i in range(period, idx + 1):
            atr = (atr * (period - 1) + self._tr[i]) / period
        return atr

    # -----------------------------------------------------------------------
    # Bucketing
    # -----------------------------------------------------------------------

    def _bucket(self, ratio_pct: float) -> str:
        if ratio_pct < self.config.low_threshold_pct:
            return REGIME_LOW
        if ratio_pct > self.config.high_threshold_pct:
            return REGIME_HIGH
        return REGIME_NORMAL

    # -----------------------------------------------------------------------
    # Phase 6.3.1b-B Q6.4 — Trend regime detection (production contract)
    # -----------------------------------------------------------------------

    # Backward-compat alias — old code that wants vol semantics can use this name
    detect_volatility_regime = detect_at

    def detect_trend_regime(self, timestamp: datetime) -> str:
        """Return trend regime using 1h bars STRICTLY before `timestamp`.

        Returns one of "BULLISH" / "NEUTRAL" / "BEARISH" (production contract —
        matches `BaseStrategy.apply_risk_gates(regime == "BEARISH")` check).

        Computation: 200×1h MA slope over 24h lookback.
          MA_now  = mean(close_h1[-200:])
          MA_back = mean(close_h1[-(200+24):-24])
          slope_pct = (MA_now - MA_back) / MA_back

        Returns "NEUTRAL" during warmup (insufficient 1h bars).

        Anti-lookahead: only 1h bars with timestamp STRICTLY < query_timestamp
        are visible.
        """
        if timestamp in self._trend_cache:
            return self._trend_cache[timestamp]

        h1_idx = bisect.bisect_left(self._h1_timestamps, timestamp)
        period = self.config.trend_ma_period_h1
        lookback = self.config.trend_slope_lookback_h1

        if h1_idx < period + lookback:
            self._trend_cache[timestamp] = TREND_NEUTRAL
            return TREND_NEUTRAL

        closes = [b.close for b in self._h1_bars[h1_idx - period - lookback : h1_idx]]
        # MA over last `period` 1h closes
        ma_now = sum(closes[-period:]) / period
        # MA over period ending `lookback` bars ago
        ma_back = sum(closes[-(period + lookback):-lookback]) / period

        if ma_back <= 0:
            self._trend_cache[timestamp] = TREND_NEUTRAL
            return TREND_NEUTRAL

        slope_pct = (ma_now - ma_back) / ma_back

        if slope_pct >= self.config.bullish_slope_threshold:
            result = TREND_BULLISH
        elif slope_pct <= self.config.bearish_slope_threshold:
            result = TREND_BEARISH
        else:
            result = TREND_NEUTRAL

        self._trend_cache[timestamp] = result
        return result

    def get_trend_slope_pct_at(self, timestamp: datetime) -> Optional[float]:
        """Diagnostic — return raw MA slope % at timestamp (None if warmup)."""
        h1_idx = bisect.bisect_left(self._h1_timestamps, timestamp)
        period = self.config.trend_ma_period_h1
        lookback = self.config.trend_slope_lookback_h1
        if h1_idx < period + lookback:
            return None
        closes = [b.close for b in self._h1_bars[h1_idx - period - lookback : h1_idx]]
        ma_now = sum(closes[-period:]) / period
        ma_back = sum(closes[-(period + lookback):-lookback]) / period
        if ma_back <= 0:
            return None
        return (ma_now - ma_back) / ma_back

    # -----------------------------------------------------------------------
    # 5m → 1h resampling (same logic as IndicatorsProvider, copied here to
    # keep RegimeDetector self-contained and avoid cross-module dependency)
    # -----------------------------------------------------------------------

    @staticmethod
    def _resample_to_1h(bars_5m: Sequence[Bar]) -> List[Bar]:
        """Aggregate 5m bars into 1h bars by floor-to-hour grouping.

        Same convention as IndicatorsProvider — 1h bar timestamp = window end.
        Bar at exactly hour boundary belongs to the window ending at that hour.
        """
        if not bars_5m:
            return []
        from datetime import timedelta
        h1_step = timedelta(hours=1)
        h1_bars: List[Bar] = []
        current_window_end: Optional[datetime] = None
        window_bars: List[Bar] = []

        for bar in bars_5m:
            ts = bar.timestamp
            floor_h = ts.replace(minute=0, second=0, microsecond=0)
            window_end = floor_h + h1_step if ts > floor_h else floor_h

            if current_window_end is None:
                current_window_end = window_end
                window_bars = [bar]
            elif window_end == current_window_end:
                window_bars.append(bar)
            else:
                if window_bars:
                    h1_bars.append(RegimeDetector._aggregate_window(window_bars, current_window_end))
                current_window_end = window_end
                window_bars = [bar]

        if window_bars and current_window_end is not None:
            h1_bars.append(RegimeDetector._aggregate_window(window_bars, current_window_end))

        return h1_bars

    @staticmethod
    def _aggregate_window(window_bars: List[Bar], window_end: datetime) -> Bar:
        return Bar(
            timestamp=window_end,
            open=window_bars[0].open,
            high=max(b.high for b in window_bars),
            low=min(b.low for b in window_bars),
            close=window_bars[-1].close,
            volume=sum(b.volume for b in window_bars),
        )


# ---------------------------------------------------------------------------
# Convenience: factory that builds detector + provider callable
# ---------------------------------------------------------------------------

def make_regime_provider(
    bars: Sequence[Bar],
    config: Optional[RegimeConfig] = None,
):
    """Return a callable for ReplayEngineV2.regime_provider — VOLATILITY semantics.

    Returns LOW_VOL / NORMAL / HIGH_VOL strings. Used by Mock strategies in
    Step 5a/5c-A tests. For production-contract trend regime, use
    `make_trend_regime_provider()` instead.
    """
    detector = RegimeDetector(bars, config=config)
    return detector.detect_at


def make_trend_regime_provider(
    bars: Sequence[Bar],
    config: Optional[RegimeConfig] = None,
):
    """Return a callable for ReplayEngineV2.regime_provider — TREND semantics.

    Returns BULLISH / NEUTRAL / BEARISH strings (Phase 6.3.1b-B Q6.4 —
    matches production `BaseStrategy.apply_risk_gates(regime == "BEARISH")`).

    Use this provider with `RealMeanReversionAdapter` for honest walk-forward
    verdict. Synthetic `make_regime_provider` (vol-based) used in Mock tests.
    """
    detector = RegimeDetector(bars, config=config)
    return detector.detect_trend_regime
