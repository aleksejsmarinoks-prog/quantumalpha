"""DcaDipsAdapter tests + smoke walk-forward (Phase 6.3.1a Step 5c-A)."""

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
    SnapshotContext,
)
from bot.backtest.adapters import (
    DcaDipsAdapter, MockDcaDipsStrategy, SessionTracker,
)
from bot.backtest.adapters.dca_dips_adapter import (
    MockSignal, MockSignalType,
    TIER_1_DRAWDOWN_PCT, TIER_1_DRAWDOWN_PCT_BOOST,
    TIER_2_DRAWDOWN_PCT, TIER_3_DRAWDOWN_PCT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    TIER_1_SIZE_PCT, TIER_2_SIZE_PCT, TIER_3_SIZE_PCT,
    MACRO_BOOST_WINDOW,
)


UTC = timezone.utc
T0 = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)   # UTC midnight = session start


# ---------------------------------------------------------------------------
# Bar builders
# ---------------------------------------------------------------------------

def _bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Bar:
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def _flat_day_bars(start_ts: datetime = T0, price: float = 2000.0) -> List[Bar]:
    """24 hours of 5m bars (288 bars) at constant price."""
    return [_bar(start_ts + timedelta(minutes=5 * i),
                 price, price + 0.5, price - 0.5, price)
            for i in range(288)]


def _dipping_day_bars(start_ts: datetime = T0, base: float = 2000.0,
                       drop_pct: float = 0.05, drop_at_bar: int = 100) -> List[Bar]:
    """24h bars where price drops `drop_pct` linearly starting from `drop_at_bar`."""
    bars = []
    target = base * (1 + drop_pct)   # drop_pct negative for drop
    for i in range(288):
        if i < drop_at_bar:
            price = base
        else:
            # Linear ramp from base to target over (288 - drop_at_bar) bars
            progress = (i - drop_at_bar) / (288 - drop_at_bar)
            price = base + (target - base) * progress
        bars.append(_bar(start_ts + timedelta(minutes=5 * i),
                          price, price + 0.5, price - 0.5, price))
    return bars


# ===========================================================================
# SessionTracker unit tests
# ===========================================================================

class TestSessionTracker:

    def test_session_open_after_observe(self):
        tracker = SessionTracker()
        bar = _bar(T0, 2000, 2001, 1999, 2000)
        tracker.observe("ETHUSDT", bar)
        assert tracker.session_open("ETHUSDT", T0) == 2000

    def test_session_open_persists_within_day(self):
        """First bar of day sets open; later bars same day don't override."""
        tracker = SessionTracker()
        bar1 = _bar(T0, 2000, 2001, 1999, 2000)
        bar2 = _bar(T0 + timedelta(hours=12), 1950, 1951, 1949, 1950)
        tracker.observe("ETHUSDT", bar1)
        tracker.observe("ETHUSDT", bar2)
        # Session open at any time on T0's day is bar1.open = 2000
        assert tracker.session_open("ETHUSDT", T0 + timedelta(hours=12)) == 2000

    def test_session_open_resets_next_day(self):
        tracker = SessionTracker()
        day1 = _bar(T0, 2000, 2001, 1999, 2000)
        day2 = _bar(T0 + timedelta(days=1), 1950, 1951, 1949, 1950)
        tracker.observe("ETHUSDT", day1)
        tracker.observe("ETHUSDT", day2)
        assert tracker.session_open("ETHUSDT", T0) == 2000
        assert tracker.session_open("ETHUSDT", T0 + timedelta(days=1)) == 1950

    def test_drawdown_computation(self):
        tracker = SessionTracker()
        tracker.observe("ETHUSDT", _bar(T0, 2000, 2001, 1999, 2000))
        # Current price 1900 → drawdown = -5%
        dd = tracker.drawdown("ETHUSDT", T0 + timedelta(hours=2), 1900)
        assert dd == pytest.approx(-0.05)

    def test_no_session_open_returns_none(self):
        tracker = SessionTracker()
        assert tracker.session_open("ETHUSDT", T0) is None
        assert tracker.drawdown("ETHUSDT", T0, 2000) is None

    def test_per_symbol_isolation(self):
        tracker = SessionTracker()
        tracker.observe("ETHUSDT", _bar(T0, 2000, 2001, 1999, 2000))
        tracker.observe("SOLUSDT", _bar(T0, 150, 151, 149, 150))
        assert tracker.session_open("ETHUSDT", T0) == 2000
        assert tracker.session_open("SOLUSDT", T0) == 150


# ===========================================================================
# Mock strategy behavior
# ===========================================================================

class TestMockDcaDipsStrategy:

    def test_tier_1_triggers_at_minus_2pct(self):
        strat = MockDcaDipsStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": -0.025,   # -2.5%
            "has_high_importance_macro": False,
            "last_price": 1950,
        }, "NORMAL")
        assert sig.signal_type == MockSignalType.ENTER_LONG
        assert sig.metadata["tier"] == 1

    def test_tier_1_no_trigger_above_threshold(self):
        strat = MockDcaDipsStrategy()
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": -0.015,   # -1.5%, between boost and normal threshold
            "has_high_importance_macro": False,
            "last_price": 1970,
        }, "NORMAL")
        assert sig.signal_type == MockSignalType.HOLD

    def test_macro_boost_tightens_tier_1_to_minus_1pct(self):
        strat = MockDcaDipsStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": -0.012,   # -1.2%
            "has_high_importance_macro": True,
            "last_price": 1976,
        }, "NORMAL")
        assert sig.signal_type == MockSignalType.ENTER_LONG
        assert "macro_boost" in sig.reason

    def test_tier_2_scale_in_at_minus_5pct(self):
        strat = MockDcaDipsStrategy()
        strat.set_strategy_capital(1000.0)
        strat._position_tiers["ETHUSDT"] = 1
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": -0.07,
            "has_high_importance_macro": False,
            "last_price": 1860,
        }, "NORMAL")
        assert sig.signal_type == MockSignalType.SCALE_IN
        assert sig.metadata["tier"] == 2

    def test_tier_3_scale_in_at_minus_10pct(self):
        strat = MockDcaDipsStrategy()
        strat.set_strategy_capital(1000.0)
        strat._position_tiers["ETHUSDT"] = 2
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": -0.12,
            "has_high_importance_macro": False,
            "last_price": 1760,
        }, "NORMAL")
        assert sig.signal_type == MockSignalType.SCALE_IN
        assert sig.metadata["tier"] == 3

    def test_no_drawdown_data_returns_hold(self):
        strat = MockDcaDipsStrategy()
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": None,
            "last_price": 2000,
        }, "NORMAL")
        assert sig.signal_type == MockSignalType.HOLD

    def test_high_vol_regime_gates_to_hold(self):
        strat = MockDcaDipsStrategy()
        strat.set_strategy_capital(1000.0)
        sig = strat.evaluate("ETHUSDT", {
            "session_drawdown_pct": -0.03,
            "has_high_importance_macro": False,
            "last_price": 1940,
        }, "NORMAL")
        gated = strat.apply_risk_gates(sig, "HIGH_VOL")
        assert gated.signal_type == MockSignalType.HOLD


# ===========================================================================
# Adapter construction + prepare_market_data
# ===========================================================================

class TestAdapterConstruction:

    def test_default_uses_mock(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        assert isinstance(adapter.strategy, MockDcaDipsStrategy)
        assert adapter.name == "dca_dips"

    def test_session_tracker_initialized(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        assert isinstance(adapter._sessions, SessionTracker)

    def test_stop_loss_take_profit(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        assert adapter.get_stop_loss_pct(None, {}) == STOP_LOSS_PCT == 0.05
        assert adapter.get_take_profit_pct(None, {}) == TAKE_PROFIT_PCT == 0.06


class TestPrepareMarketData:

    def test_first_bar_of_day_registers_session_open(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        bar = _bar(T0, 2000, 2001, 1999, 2000)
        snap = SnapshotContext(
            timestamp=T0, symbol="ETHUSDT", bar=bar,
            spot=2000, equity=100, open_position_count=0, regime="NORMAL",
        )
        md = adapter.prepare_market_data(snap)
        assert md is not None
        assert md["session_drawdown_pct"] == 0.0   # at session open

    def test_drawdown_calculated_correctly(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        # First bar establishes session open at 2000
        bar1 = _bar(T0, 2000, 2001, 1999, 2000)
        snap1 = SnapshotContext(
            timestamp=T0, symbol="ETHUSDT", bar=bar1,
            spot=2000, equity=100, open_position_count=0, regime="NORMAL",
        )
        adapter.prepare_market_data(snap1)

        # Second bar — 5% drop
        bar2 = _bar(T0 + timedelta(hours=1), 1900, 1901, 1899, 1900)
        snap2 = SnapshotContext(
            timestamp=T0 + timedelta(hours=1), symbol="ETHUSDT", bar=bar2,
            spot=1900, equity=100, open_position_count=0, regime="NORMAL",
        )
        md = adapter.prepare_market_data(snap2)
        assert md["session_drawdown_pct"] == pytest.approx(-0.05)

    def test_high_importance_macro_detected(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        bar = _bar(T0 + timedelta(hours=3), 2000, 2001, 1999, 2000)
        # Macro event 1h ago, high importance
        events = (
            {"time_utc": (T0 + timedelta(hours=2)).isoformat(),
             "name": "FOMC", "importance": "high"},
        )
        snap = SnapshotContext(
            timestamp=T0 + timedelta(hours=3), symbol="ETHUSDT", bar=bar,
            spot=2000, equity=100, open_position_count=0, regime="NORMAL",
            macro_events=events,
        )
        md = adapter.prepare_market_data(snap)
        assert md["has_high_importance_macro"] is True

    def test_old_macro_event_outside_window_ignored(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        bar = _bar(T0 + timedelta(hours=5), 2000, 2001, 1999, 2000)
        # Macro event 4h ago (outside 2h window)
        events = (
            {"time_utc": (T0 + timedelta(hours=1)).isoformat(),
             "name": "FOMC", "importance": "high"},
        )
        snap = SnapshotContext(
            timestamp=T0 + timedelta(hours=5), symbol="ETHUSDT", bar=bar,
            spot=2000, equity=100, open_position_count=0, regime="NORMAL",
            macro_events=events,
        )
        md = adapter.prepare_market_data(snap)
        assert md["has_high_importance_macro"] is False

    def test_low_importance_macro_ignored(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        bar = _bar(T0 + timedelta(hours=1), 2000, 2001, 1999, 2000)
        events = (
            {"time_utc": T0.isoformat(), "name": "PMI", "importance": "low"},
        )
        snap = SnapshotContext(
            timestamp=T0 + timedelta(hours=1), symbol="ETHUSDT", bar=bar,
            spot=2000, equity=100, open_position_count=0, regime="NORMAL",
            macro_events=events,
        )
        md = adapter.prepare_market_data(snap)
        assert md["has_high_importance_macro"] is False


# ===========================================================================
# Reset (walk-forward window boundary)
# ===========================================================================

class TestReset:

    def test_reset_clears_sessions(self):
        adapter = DcaDipsAdapter(starting_capital_usd=100.0)
        bar = _bar(T0, 2000, 2001, 1999, 2000)
        snap = SnapshotContext(
            timestamp=T0, symbol="ETHUSDT", bar=bar,
            spot=2000, equity=100, open_position_count=0, regime="NORMAL",
        )
        adapter.prepare_market_data(snap)
        assert adapter._sessions.session_open("ETHUSDT", T0) is not None

        adapter.reset()
        assert adapter._sessions.session_open("ETHUSDT", T0) is None


# ===========================================================================
# End-to-end with engine
# ===========================================================================

class TestEndToEnd:

    def test_flat_day_no_trades(self):
        """Flat market → no drawdown → no entries."""
        bars = _flat_day_bars()
        adapter = DcaDipsAdapter(starting_capital_usd=200.0)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            slippage_bps=0, commission_bps=0,
        )
        result = eng.run(bars, adapter)
        assert result.trade_count == 0

    def test_dipping_day_triggers_entries(self):
        """Day with -8% drawdown → tier 1 ENTER_LONG and tier 2 SCALE_IN."""
        bars = _dipping_day_bars(drop_pct=-0.08, drop_at_bar=50)
        adapter = DcaDipsAdapter(starting_capital_usd=200.0)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            slippage_bps=0, commission_bps=0,
        )
        result = eng.run(bars, adapter)

        stats = adapter.get_stats()
        # At least one signal executed (entry triggered by drawdown)
        assert stats["signals_executed"] >= 1
        # Kernel approved at least one
        assert stats["risk_kernel_status"]["approvals_count"] >= 1


# ===========================================================================
# SMOKE WALK-FORWARD: 1 week ETH 5m
# ===========================================================================

class TestSmokeWalkForward:

    def _synthetic_week(self, seed: int = 42) -> List[Bar]:
        """1 week with realistic intraday drawdowns and recoveries.

        Designed to produce some -2% intraday dips so tier 1 fires."""
        rng = random.Random(seed)
        bars: List[Bar] = []
        price = 2000.0
        for i in range(2016):
            # Session boundary every 288 bars (24h)
            session_idx = i % 288
            # Higher vol mid-day, calmer overnight
            sigma = 6.0 if 100 <= session_idx <= 250 else 2.0
            change = rng.gauss(0, sigma)
            next_close = max(100, price + change)
            high = max(price, next_close) + abs(change) * 0.3
            low = min(price, next_close) - abs(change) * 0.3
            bars.append(_bar(
                T0 + timedelta(minutes=5 * i),
                price, high, low, next_close,
            ))
            price = next_close
        return bars

    def test_one_week_runs_without_exceptions(self):
        bars = self._synthetic_week()
        assert len(bars) == 2016
        adapter = DcaDipsAdapter(starting_capital_usd=200.0)
        regime = make_regime_provider(bars)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            regime_provider=regime,
        )

        result = eng.run(bars, adapter)

        assert result.bars_processed == 2016
        assert math.isfinite(result.final_equity)
        assert result.final_equity > 50.0
        assert result.final_equity < 1000.0
        assert len(result.equity_curve) == 2016

    def test_one_week_smoke_telemetry_breakdown(self):
        bars = self._synthetic_week()
        adapter = DcaDipsAdapter(starting_capital_usd=200.0)
        regime = make_regime_provider(bars)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            regime_provider=regime,
        )
        result = eng.run(bars, adapter)
        stats = adapter.get_stats()
        kernel_status = stats["risk_kernel_status"]

        print("\n=== DcaDips smoke walk-forward telemetry ===")
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
        print("=============================================\n")
