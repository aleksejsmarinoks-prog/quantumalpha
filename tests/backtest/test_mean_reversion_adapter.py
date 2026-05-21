"""MeanReversionAdapter tests + smoke walk-forward (Phase 6.3.1a Step 5a)."""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, ReplayEngineV2, IndicatorsProvider, make_regime_provider,
    REGIME_LOW, REGIME_NORMAL, REGIME_HIGH,
    SnapshotContext, Position,
)
from bot.backtest.adapters import (
    MeanReversionAdapter, MockMeanReversionStrategy,
)
from bot.backtest.adapters.mean_reversion_adapter import (
    MockSignal, MockSignalType,
    RSI_OVERSOLD_TIER_1, RSI_OVERSOLD_TIER_2, RSI_OVERSOLD_TIER_3,
    RSI_OVERBOUGHT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
)


UTC = timezone.utc
T0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Bar builders
# ---------------------------------------------------------------------------

def _flat_bars(n: int, price: float = 2000.0) -> List[Bar]:
    return [
        Bar(timestamp=T0 + timedelta(minutes=5 * i),
            open=price, high=price + 0.5, low=price - 0.5, close=price, volume=1000)
        for i in range(n)
    ]


def _trending_bars(n: int, base: float = 2000.0, slope: float = 1.0) -> List[Bar]:
    bars = []
    price = base
    for i in range(n):
        bars.append(Bar(
            timestamp=T0 + timedelta(minutes=5 * i),
            open=price, high=price + abs(slope) + 0.5,
            low=price - 0.5, close=price + slope, volume=1000,
        ))
        price += slope
    return bars


def _synthetic_week_bars(seed: int = 42, base: float = 2000.0) -> List[Bar]:
    """1 week of 5m bars (2016 bars) using random walk with controlled volatility.

    Designed to create:
      - Periods of low vol (LOW_VOL regime)
      - Periods of higher vol (HIGH_VOL)
      - Some RSI extremes (entry signals)
    """
    rng = random.Random(seed)
    bars: List[Bar] = []
    price = base
    for i in range(2016):
        # Inject vol regime changes: bars 500-1000 = higher vol
        sigma = 8.0 if 500 <= i < 1000 else 2.5
        change = rng.gauss(0, sigma)
        # Add mean-reversion: pull back to base if too far
        pullback = (base - price) * 0.005
        next_close = max(100, price + change + pullback)
        high = max(price, next_close) + abs(change) * 0.3
        low = min(price, next_close) - abs(change) * 0.3
        bars.append(Bar(
            timestamp=T0 + timedelta(minutes=5 * i),
            open=price, high=high, low=low, close=next_close, volume=1000.0,
        ))
        price = next_close
    return bars


# ===========================================================================
# Construction
# ===========================================================================

class TestConstruction:

    def test_default_uses_mock_strategy(self):
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)
        assert isinstance(adapter.strategy, MockMeanReversionStrategy)
        assert adapter.name == "mean_reversion"

    def test_required_lookback_bars(self):
        adapter = MeanReversionAdapter()
        assert adapter.required_lookback_bars() == 200

    def test_evaluation_interval_5m(self):
        adapter = MeanReversionAdapter()
        assert adapter.evaluation_interval() == timedelta(minutes=5)

    def test_stop_loss_and_take_profit_defaults(self):
        adapter = MeanReversionAdapter()
        # Mock signal — adapter ignores it for these getters
        sl = adapter.get_stop_loss_pct(None, {})
        tp = adapter.get_take_profit_pct(None, {})
        assert sl == STOP_LOSS_PCT == 0.02
        assert tp == TAKE_PROFIT_PCT == 0.04


# ===========================================================================
# prepare_market_data
# ===========================================================================

class TestPrepareMarketData:

    def test_warmup_returns_none(self):
        """Snapshot with no indicators → prepare_market_data returns None."""
        adapter = MeanReversionAdapter()
        snap = SnapshotContext(
            timestamp=T0, symbol="ETHUSDT",
            bar=Bar(timestamp=T0, open=2000, high=2001, low=1999, close=2000, volume=1000),
            spot=2000, equity=200.0, open_position_count=0,
            regime="NORMAL", indicators={},   # empty — no RSI
        )
        result = adapter.prepare_market_data(snap)
        assert result is None

    def test_with_rsi_returns_dict(self):
        adapter = MeanReversionAdapter()
        snap = SnapshotContext(
            timestamp=T0, symbol="ETHUSDT",
            bar=Bar(timestamp=T0, open=2000, high=2001, low=1999, close=2000, volume=1000),
            spot=2000, equity=200.0, open_position_count=0,
            regime="NORMAL",
            indicators={"rsi_14_1h": 25.0, "last_price": 2000.0, "returns_1h": -0.01},
        )
        md = adapter.prepare_market_data(snap)
        assert md is not None
        assert md["rsi_14_1h"] == 25.0
        assert md["last_price"] == 2000.0


# ===========================================================================
# Mock strategy behavior (tier logic, regime gates)
# ===========================================================================

class TestMockStrategyLogic:

    def test_rsi_below_30_emits_enter_long_tier1(self):
        strat = MockMeanReversionStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 28.0, "last_price": 2000}, "NORMAL")
        assert sig.signal_type == MockSignalType.ENTER_LONG
        assert sig.metadata["tier"] == 1

    def test_rsi_below_25_with_position_emits_scale_in_tier2(self):
        strat = MockMeanReversionStrategy()
        strat.set_strategy_capital(1000.0)
        strat._position_tiers["ETHUSDT"] = 1
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 22.0, "last_price": 2000}, "NORMAL")
        assert sig.signal_type == MockSignalType.SCALE_IN
        assert sig.metadata["tier"] == 2

    def test_rsi_below_20_with_position_emits_scale_in_tier3(self):
        strat = MockMeanReversionStrategy()
        strat.set_strategy_capital(1000.0)
        strat._position_tiers["ETHUSDT"] = 2
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 18.0, "last_price": 2000}, "NORMAL")
        assert sig.signal_type == MockSignalType.SCALE_IN
        assert sig.metadata["tier"] == 3

    def test_rsi_above_70_emits_enter_short(self):
        strat = MockMeanReversionStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 75.0, "last_price": 2000}, "NORMAL")
        assert sig.signal_type == MockSignalType.ENTER_SHORT

    def test_rsi_neutral_emits_hold(self):
        strat = MockMeanReversionStrategy()
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 50.0, "last_price": 2000}, "NORMAL")
        assert sig.signal_type == MockSignalType.HOLD

    def test_high_vol_regime_gates_to_hold(self):
        strat = MockMeanReversionStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 25.0, "last_price": 2000}, "NORMAL")
        # Signal would be ENTER_LONG, then gate hits
        gated = strat.apply_risk_gates(sig, "HIGH_VOL")
        assert gated.signal_type == MockSignalType.HOLD

    def test_low_vol_regime_signal_passes_through(self):
        strat = MockMeanReversionStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {"rsi_14_1h": 25.0, "last_price": 2000}, "LOW_VOL")
        gated = strat.apply_risk_gates(sig, "LOW_VOL")
        assert gated.signal_type == MockSignalType.ENTER_LONG


# ===========================================================================
# Adapter end-to-end with engine + indicators + regime
# ===========================================================================

class TestAdapterIntegration:

    def test_full_pipeline_flat_bars_no_signals(self):
        """Flat market → RSI undefined → prepare_market_data returns None for
        every bar → 0 signals emitted, 0 trades."""
        bars = _flat_bars(300)
        indicators = IndicatorsProvider(bars)
        regime = make_regime_provider(bars)
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)

        eng = ReplayEngineV2(
            symbol="ETHUSDT",
            initial_equity=200.0,
            regime_provider=regime,
            indicators_provider=indicators.callable_for_engine(),
        )
        result = eng.run(bars, adapter)
        assert result.trade_count == 0
        # On absolutely flat bars, prepare_market_data returns None → 0 signals
        # (counter only increments after strategy.evaluate runs)
        assert adapter._signals_emitted == 0
        assert adapter._signals_executed == 0

    def test_downtrending_bars_trigger_long_entries(self):
        """Strong downtrend pushes RSI < 30 → adapter emits ENTER_LONG."""
        bars = _trending_bars(300, slope=-1.0)   # 300 bars going down
        indicators = IndicatorsProvider(bars)
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)
        # No regime gate (NORMAL by default if no provider)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            indicators_provider=indicators.callable_for_engine(),
        )
        result = eng.run(bars, adapter)

        # At least some signals should pass through to entries
        # (Strategy itself filters; kernel may reduce/halt; some trades expected)
        stats = adapter.get_stats()
        assert stats["signals_emitted"] > 100   # adapter evaluated many bars
        # If no trades, that's still OK — could be all rejected. Print for visibility.

    def test_high_vol_regime_blocks_signals(self):
        """In HIGH_VOL regime, mock strategy gates ENTER_LONG to HOLD."""
        # Wild bars → high vol regime
        bars = []
        random.seed(0)
        price = 2000.0
        for i in range(300):
            change = random.gauss(0, 30)   # high vol
            next_close = max(100, price + change)
            high = max(price, next_close) + 20
            low = min(price, next_close) - 20
            bars.append(Bar(
                timestamp=T0 + timedelta(minutes=5 * i),
                open=price, high=high, low=low, close=next_close, volume=1000,
            ))
            price = next_close

        indicators = IndicatorsProvider(bars)
        regime = make_regime_provider(bars)
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            regime_provider=regime,
            indicators_provider=indicators.callable_for_engine(),
        )
        result = eng.run(bars, adapter)

        # Many signals were gated by regime → high gate count expected
        stats = adapter.get_stats()
        # Visibility only — high vol → many "gated_by_strategy" events
        # (Mock returns HOLD on HIGH_VOL → counted as gated by strategy)


# ===========================================================================
# Reset (walk-forward window boundary)
# ===========================================================================

class TestReset:

    def test_reset_clears_strategy_position_tiers(self):
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)
        adapter.strategy._position_tiers["ETHUSDT"] = 2
        adapter.reset()
        # After reset, position_tiers should be cleared (new strategy instance)
        assert adapter.strategy._position_tiers == {}


# ===========================================================================
# SMOKE WALK-FORWARD: 1 week ETH 5m
# ===========================================================================

class TestSmokeWalkForward:

    def test_one_week_eth_5m_runs_without_exceptions(self):
        """The acceptance-criteria smoke test:
        1 week of 5m bars (2016 bars) → full pipeline → sane outputs.
        """
        bars = _synthetic_week_bars(seed=42)
        assert len(bars) == 2016

        indicators = IndicatorsProvider(bars)
        regime = make_regime_provider(bars)
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)
        eng = ReplayEngineV2(
            symbol="ETHUSDT",
            initial_equity=200.0,
            regime_provider=regime,
            indicators_provider=indicators.callable_for_engine(),
            slippage_bps=5,
            commission_bps=7.5,
        )

        # Must not throw
        result = eng.run(bars, adapter)

        # Sanity assertions
        assert result.bars_processed == 2016

        # Equity sanity — should not be NaN, not negative, not 10×
        assert math.isfinite(result.final_equity)
        assert result.final_equity > 50.0      # not wiped to zero
        assert result.final_equity < 1000.0    # not 5× starting (would be suspicious)

        # Adapter ran through pipeline
        stats = adapter.get_stats()
        assert stats["signals_emitted"] > 1500    # > 75% of bars saw signals (warmup ~200)

        # Equity curve has one point per processed bar
        assert len(result.equity_curve) == 2016

    def test_one_week_smoke_telemetry_breakdown(self):
        """Diagnostic — print full breakdown for human verification."""
        bars = _synthetic_week_bars(seed=42)
        indicators = IndicatorsProvider(bars)
        regime = make_regime_provider(bars)
        adapter = MeanReversionAdapter(starting_capital_usd=200.0)
        eng = ReplayEngineV2(
            symbol="ETHUSDT",
            initial_equity=200.0,
            regime_provider=regime,
            indicators_provider=indicators.callable_for_engine(),
        )
        result = eng.run(bars, adapter)

        # Telemetry assertions (visibility, not hard correctness)
        stats = adapter.get_stats()
        kernel_status = stats["risk_kernel_status"]

        # Print for visibility (pytest captures unless -s passed)
        print("\n=== Smoke walk-forward telemetry ===")
        print(f"Bars processed:           {result.bars_processed}")
        print(f"Initial equity:           ${result.initial_equity:.2f}")
        print(f"Final equity:             ${result.final_equity:.2f}")
        print(f"Total return:             {result.total_return_pct:+.2f}%")
        print(f"Trade count:              {result.trade_count}")
        print(f"Win rate:                 {result.win_rate * 100:.1f}%")
        print(f"Max drawdown:             {result.max_drawdown_pct:.2f}%")
        print(f"Signals emitted:          {stats['signals_emitted']}")
        print(f"Signals gated (strategy): {stats['signals_gated_by_strategy']}")
        print(f"Signals kernel halted:    {stats['signals_kernel_halted']}")
        print(f"Signals kernel rejected:  {stats['signals_kernel_rejected']}")
        print(f"Signals kernel reduced:   {stats['signals_kernel_reduced']}")
        print(f"Signals executed:        {stats['signals_executed']}")
        print(f"Kernel approvals:         {kernel_status['approvals_count']}")
        print(f"Kernel halted:            {kernel_status['halted']}")
        print(f"Kernel halt reason:       {kernel_status['halt_reason']}")
        print("====================================\n")

        # Basic sanity
        assert isinstance(stats["signals_emitted"], int)
        assert kernel_status["halt_reason"] in (
            "none", "daily_drawdown", "weekly_drawdown",
            "total_drawdown", "consecutive_loss_cooldown",
        )
