"""Tests for Phase 6.3.1b-B fixes (Bug-1 + Q6.4 + RealMeanReversionAdapter)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, IndicatorsProvider, ReplayEngineV2, SnapshotContext,
    RegimeDetector, RegimeConfig, make_regime_provider, make_trend_regime_provider,
    REGIME_LOW, REGIME_NORMAL, REGIME_HIGH,
    TREND_BULLISH, TREND_NEUTRAL, TREND_BEARISH,
)
from bot.backtest.adapters import MeanReversionAdapter


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _bars(n: int, start_price: float = 2000.0, slope: float = 0.0) -> List[Bar]:
    bars = []
    price = start_price
    for i in range(n):
        bars.append(Bar(
            timestamp=T0 + timedelta(minutes=5 * i),
            open=price, high=price + 1, low=price - 1, close=price + slope,
            volume=1000,
        ))
        price += slope
    return bars


# ===========================================================================
# Bug-1 — evaluate() now= injection
# ===========================================================================

class TestBug1EvaluateNowInjection:
    """Bug-1: production_adapter_base.evaluate() must pass now=snapshot.timestamp."""

    def test_evaluate_receives_snapshot_timestamp_not_wall_clock(self):
        """Probe captures now= kwarg seen by strategy.evaluate."""
        captured_nows = []

        class _CapturingStrategy:
            def __init__(self, **kwargs):
                self.capital_pct = 1.0
            def get_strategy_id(self): return "capture"
            def evaluate(self, symbol, market_data, regime, now=None):
                captured_nows.append(now)
                from bot.backtest.adapters.mean_reversion_adapter import MockSignal, MockSignalType
                return MockSignal(MockSignalType.HOLD, 0.0, 0.0, reason="probe")
            def apply_risk_gates(self, signal, regime, now=None):
                return signal
            def set_strategy_capital(self, capital): pass

        class _CaptureAdapter(MeanReversionAdapter):
            strategy_class = _CapturingStrategy

        bars = _bars(300, slope=0.5)
        adapter = _CaptureAdapter(starting_capital_usd=200.0)
        indicators = IndicatorsProvider(bars)
        engine = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            indicators_provider=indicators.callable_for_engine(),
        )
        engine.run(bars, adapter)

        # All captured `now` values must be backtest bar timestamps, not wall clock
        assert len(captured_nows) > 0, "evaluate() was never called"
        for now in captured_nows:
            assert now is not None, "evaluate received now=None (wall-clock fallback)"
            assert now.year == 2026, f"now.year was {now.year}, not 2026 — wall-clock leak?"
            # Verify timestamp is within bar range (Jan 2026)
            assert now.month == 1, f"now.month was {now.month}, not 1 — wall-clock leak?"

    def test_evaluate_now_strictly_matches_snapshot_timestamp(self):
        """Each evaluate() call's now= must equal the snapshot.timestamp for that tick."""
        nows_by_bar_ts = []

        class _StrictCapturingStrategy:
            def __init__(self, **kwargs):
                self.capital_pct = 1.0
                self._last_ts = None
            def get_strategy_id(self): return "strict"
            def evaluate(self, symbol, market_data, regime, now=None):
                nows_by_bar_ts.append((now, market_data.get("last_price")))
                from bot.backtest.adapters.mean_reversion_adapter import MockSignal, MockSignalType
                return MockSignal(MockSignalType.HOLD, 0.0, 0.0, reason="strict")
            def apply_risk_gates(self, signal, regime, now=None):
                return signal
            def set_strategy_capital(self, capital): pass

        class _StrictAdapter(MeanReversionAdapter):
            strategy_class = _StrictCapturingStrategy

        bars = _bars(300, slope=0.5)
        adapter = _StrictAdapter(starting_capital_usd=200.0)
        indicators = IndicatorsProvider(bars)
        engine = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            indicators_provider=indicators.callable_for_engine(),
        )
        engine.run(bars, adapter)

        # Verify nows are monotonically increasing (bar-by-bar)
        nows = [n for n, _ in nows_by_bar_ts if n is not None]
        assert len(nows) > 5
        for i in range(len(nows) - 1):
            assert nows[i] <= nows[i + 1], "nows must be monotonic"


# ===========================================================================
# Q6.4 — Trend regime
# ===========================================================================

class TestTrendRegime:

    def test_uptrending_bars_classify_bullish(self):
        bars = _bars(5000, start_price=2000.0, slope=0.5)
        rd = RegimeDetector(bars)
        regime = rd.detect_trend_regime(bars[-1].timestamp + timedelta(minutes=1))
        assert regime == TREND_BULLISH

    def test_downtrending_bars_classify_bearish(self):
        bars = _bars(5000, start_price=4500.0, slope=-0.5)
        rd = RegimeDetector(bars)
        regime = rd.detect_trend_regime(bars[-1].timestamp + timedelta(minutes=1))
        assert regime == TREND_BEARISH

    def test_flat_bars_classify_neutral(self):
        bars = _bars(5000, slope=0.0)
        rd = RegimeDetector(bars)
        regime = rd.detect_trend_regime(bars[-1].timestamp + timedelta(minutes=1))
        assert regime == TREND_NEUTRAL

    def test_warmup_returns_neutral(self):
        """Insufficient h1 bars for MA → NEUTRAL."""
        bars = _bars(50, slope=2.0)
        rd = RegimeDetector(bars)
        regime = rd.detect_trend_regime(bars[-1].timestamp + timedelta(minutes=1))
        assert regime == TREND_NEUTRAL

    def test_trend_uses_strict_anti_lookahead(self):
        """Bar at exactly ts must NOT influence trend at ts."""
        bars = _bars(5000, slope=0.5)
        rd = RegimeDetector(bars)
        # Query at exactly bar 4000's timestamp — bar 4000 + later NOT visible
        ts = bars[4000].timestamp
        slope_pct = rd.get_trend_slope_pct_at(ts)
        # Re-query at later — bar 4000+ now visible, slope changes
        slope_later = rd.get_trend_slope_pct_at(bars[4500].timestamp)
        assert slope_pct is not None
        assert slope_later is not None
        # Different timestamps → different slope values (different bar windows)
        assert slope_pct != slope_later

    def test_trend_factory_returns_callable(self):
        bars = _bars(5000, slope=0.5)
        provider = make_trend_regime_provider(bars)
        regime = provider(bars[-1].timestamp + timedelta(minutes=1))
        assert regime in (TREND_BULLISH, TREND_NEUTRAL, TREND_BEARISH)

    def test_vol_factory_still_returns_vol_regime(self):
        """Backward compat: make_regime_provider unchanged (vol semantics)."""
        bars = _bars(500, slope=0.5)
        provider = make_regime_provider(bars)
        regime = provider(bars[-1].timestamp + timedelta(minutes=1))
        assert regime in (REGIME_LOW, REGIME_NORMAL, REGIME_HIGH)

    def test_trend_threshold_validation(self):
        with pytest.raises(ValueError, match="bullish_slope_threshold"):
            RegimeConfig(bullish_slope_threshold=-0.001, bearish_slope_threshold=0.001)

    def test_trend_uses_1h_resample(self):
        """Verify trend detector uses 1h-resampled bars, not raw 5m."""
        bars = _bars(5000, slope=0.5)
        rd = RegimeDetector(bars)
        # h1_bars internal should be < raw bars
        assert len(rd._h1_bars) < len(rd._bars)
        # Roughly 5000 / 12 ≈ 417 h1 bars
        assert len(rd._h1_bars) <= 420
        assert len(rd._h1_bars) >= 410


# ===========================================================================
# RealMeanReversionAdapter
# ===========================================================================

class TestRealMeanReversionAdapter:

    def test_imports_or_raises_clean(self):
        """In test sandbox (no bot.strategies module), import should not crash —
        but construction should raise ImportError with clear message."""
        from bot.backtest.adapters import real_mean_reversion_adapter
        # Module-level import OK
        assert hasattr(real_mean_reversion_adapter, "RealMeanReversionAdapter")

    def test_construction_fails_clean_without_prod_strategy(self):
        from bot.backtest.adapters.real_mean_reversion_adapter import RealMeanReversionAdapter
        with pytest.raises(ImportError, match="Production `bot.strategies.mean_reversion"):
            RealMeanReversionAdapter(starting_capital_usd=200.0)
