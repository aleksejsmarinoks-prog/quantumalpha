"""Tests for bot.backtester.replay_engine — focus on no-lookahead + ordering."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import pytest

from bot.backtester.execution_sim import ExecutionSimulator
from bot.backtester.models import Signal, SignalAction, Side
from bot.backtester.replay_engine import ReplayEngine, SnapshotContext
from bot.backtester.strategy_adapters.base_adapter import BaseAdapter
from bot.backtester.strategy_adapters.mean_reversion_adapter import MeanReversionAdapter
from tests.backtester.conftest import make_klines, make_mean_reverting_series


# ─────────────────────────────────────────────────────────────────────────────
# Helper test adapters
# ─────────────────────────────────────────────────────────────────────────────

class _RecordingAdapter(BaseAdapter):
    """Adapter that records the timestamps it was called with."""
    name = "test_recording"

    def reset(self, params: dict) -> None:
        self._params = dict(params)
        self.calls: list[datetime] = []
        self.histories_max_ts: list[datetime] = []

    def required_lookback_bars(self) -> int:
        return 3

    def evaluation_interval(self) -> timedelta:
        return timedelta(minutes=5)

    def evaluate(self, ctx: SnapshotContext) -> Optional[Signal]:
        self.calls.append(ctx.now)
        if not ctx.history.empty:
            self.histories_max_ts.append(ctx.history.index[-1].to_pydatetime())
        return None


class _AlwaysBuyAdapter(BaseAdapter):
    """Adapter that emits ENTER_LONG on every eval until in a position."""
    name = "always_buy"

    def reset(self, params: dict) -> None:
        self._params = dict(params)

    def required_lookback_bars(self) -> int:
        return 2

    def evaluation_interval(self) -> timedelta:
        return timedelta(minutes=5)

    def evaluate(self, ctx: SnapshotContext) -> Optional[Signal]:
        if ctx.open_position_usd > 0:
            return None
        return Signal(
            timestamp=ctx.now, symbol=ctx.symbol,
            action=SignalAction.ENTER_LONG, size_usd=100.0,
            metadata={"maker": False},
        )


class TestNoLookahead:
    def test_history_never_exceeds_now(self, sample_klines):
        adapter = _RecordingAdapter()
        adapter.reset({})
        engine = ReplayEngine(adapter=adapter, klines={"ETHUSDT": sample_klines}, funding={})
        start = sample_klines.index[0].to_pydatetime()
        end = sample_klines.index[-1].to_pydatetime()
        engine.run(start, end, "ETHUSDT")
        # For every call, history's max ts must be ≤ now
        for now, hist_max in zip(adapter.calls, adapter.histories_max_ts):
            assert hist_max <= now

    def test_runs_chronologically(self, sample_klines):
        adapter = _RecordingAdapter()
        adapter.reset({})
        engine = ReplayEngine(adapter=adapter, klines={"ETHUSDT": sample_klines}, funding={})
        start = sample_klines.index[0].to_pydatetime()
        end = sample_klines.index[-1].to_pydatetime()
        engine.run(start, end, "ETHUSDT")
        # Timestamps must be monotonically increasing
        assert all(adapter.calls[i] <= adapter.calls[i + 1] for i in range(len(adapter.calls) - 1))


class TestSignalExecution:
    def test_enter_signal_creates_open_position_no_trade(self, sample_klines):
        adapter = _AlwaysBuyAdapter()
        adapter.reset({})
        engine = ReplayEngine(adapter=adapter, klines={"ETHUSDT": sample_klines}, funding={},
                              execution_sim=ExecutionSimulator(maker_fill_rate=1.0, seed=1))
        start = sample_klines.index[0].to_pydatetime()
        # Run very short window
        end = (sample_klines.index[0] + timedelta(minutes=30)).to_pydatetime()
        trades, equity = engine.run(start, end, "ETHUSDT")
        # No exits → no closed trades, but position should be open during equity curve
        assert len([t for t in trades if t.is_closed]) == 0
        assert not equity.empty

    def test_full_round_trip_produces_trade(self):
        # Construct a tiny series and a tiny adapter that buys then exits
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        df = make_klines(start, n_bars=10, bar_minutes=5, base_price=3500.0, volatility=0.0, seed=0)

        class _BuyThenExitAdapter(BaseAdapter):
            name = "buy_exit"
            def reset(self, params):
                self._params = params
                self._step = 0
            def required_lookback_bars(self): return 2
            def evaluation_interval(self): return timedelta(minutes=5)
            def evaluate(self, ctx):
                self._step += 1
                if self._step == 3 and ctx.open_position_usd == 0:
                    return Signal(ctx.now, ctx.symbol, SignalAction.ENTER_LONG, size_usd=100.0,
                                  metadata={"maker": False})
                if self._step >= 6 and ctx.open_position_usd > 0:
                    return Signal(ctx.now, ctx.symbol, SignalAction.EXIT, size_usd=ctx.open_position_usd)
                return None

        adapter = _BuyThenExitAdapter()
        adapter.reset({})
        engine = ReplayEngine(adapter=adapter, klines={"ETHUSDT": df}, funding={},
                              execution_sim=ExecutionSimulator(maker_fill_rate=1.0, seed=1))
        trades, equity = engine.run(df.index[0].to_pydatetime(), df.index[-1].to_pydatetime(), "ETHUSDT")
        assert len([t for t in trades if t.is_closed]) == 1
        t = trades[0]
        assert t.side == Side.BUY
        assert t.exit_fill is not None


class TestEmptyData:
    def test_empty_klines_returns_empty(self):
        adapter = _RecordingAdapter()
        adapter.reset({})
        engine = ReplayEngine(adapter=adapter, klines={"ETHUSDT": pd.DataFrame()}, funding={})
        trades, equity = engine.run(datetime(2026, 1, 1, tzinfo=timezone.utc),
                                    datetime(2026, 1, 2, tzinfo=timezone.utc), "ETHUSDT")
        assert trades == []
        assert equity.empty

    def test_unknown_symbol_returns_empty(self, sample_klines):
        adapter = _RecordingAdapter()
        adapter.reset({})
        engine = ReplayEngine(adapter=adapter, klines={"ETHUSDT": sample_klines}, funding={})
        trades, equity = engine.run(sample_klines.index[0].to_pydatetime(),
                                    sample_klines.index[-1].to_pydatetime(), "XYZ")
        assert trades == []
        assert equity.empty


class TestMeanReversionAdapterSmoke:
    def test_mean_rev_emits_signals_on_oscillating_price(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        df = make_mean_reverting_series(start, n_bars=200, base=3500.0, amplitude=70.0, cycle_bars=20)
        adapter = MeanReversionAdapter()
        adapter.reset({"lookback_bars": 10, "z_entry": 1.5, "z_exit": 0.3, "size_usd": 100.0})
        engine = ReplayEngine(
            adapter=adapter, klines={"ETHUSDT": df}, funding={},
            execution_sim=ExecutionSimulator(maker_fill_rate=1.0, seed=1),
        )
        trades, equity = engine.run(df.index[0].to_pydatetime(),
                                    df.index[-1].to_pydatetime(), "ETHUSDT")
        # Some trades should have been generated
        assert len(trades) >= 1
        assert not equity.empty
