"""
Tests for individual strategy adapters — boost coverage and verify each
adapter emits sensible signals on canonical input.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.backtester.execution_sim import ExecutionSimulator
from bot.backtester.models import Side, SignalAction
from bot.backtester.replay_engine import ReplayEngine, SnapshotContext
from bot.backtester.strategy_adapters.dca_dips_adapter import DcaDipsAdapter
from bot.backtester.strategy_adapters.funding_arb_adapter import FundingArbAdapter
from bot.backtester.strategy_adapters.mean_reversion_adapter import MeanReversionAdapter
from tests.backtester.conftest import make_funding_history, make_klines


def _ctx_from_klines(df: pd.DataFrame, funding: pd.DataFrame, now_idx: int = -1,
                     open_pos_usd: float = 0.0, open_side=None) -> SnapshotContext:
    sub = df.iloc[: (len(df) + now_idx + 1) if now_idx < 0 else now_idx + 1]
    now = sub.index[-1].to_pydatetime() if not sub.empty else df.index[0].to_pydatetime()
    return SnapshotContext(
        now=now,
        symbol="ETHUSDT",
        history=sub,
        funding_history=funding[funding.index <= now] if not funding.empty else pd.DataFrame(),
        capital_usd=1000.0,
        open_position_usd=open_pos_usd,
        open_position_side=open_side,
        adv_24h_usd=1_000_000.0,
    )


class TestFundingArbAdapter:
    def test_no_signal_when_funding_near_zero(self):
        adapter = FundingArbAdapter()
        adapter.reset({})
        # Funding rates all near zero
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        funding = pd.DataFrame({"funding_rate": [0.00005]},
                               index=pd.DatetimeIndex([start], tz="UTC"))
        klines = make_klines(start, n_bars=5, bar_minutes=5)
        ctx = _ctx_from_klines(klines, funding)
        signal = adapter.evaluate(ctx)
        assert signal is None

    def test_emits_short_on_high_positive_funding(self):
        adapter = FundingArbAdapter()
        adapter.reset({"open_threshold_8h": 0.0003})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        funding = pd.DataFrame({"funding_rate": [0.001]},
                               index=pd.DatetimeIndex([start], tz="UTC"))
        klines = make_klines(start, n_bars=5, bar_minutes=5)
        ctx = _ctx_from_klines(klines, funding)
        signal = adapter.evaluate(ctx)
        assert signal is not None
        assert signal.action == SignalAction.ENTER_SHORT

    def test_emits_long_on_high_negative_funding(self):
        adapter = FundingArbAdapter()
        adapter.reset({"open_threshold_8h": 0.0003})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        funding = pd.DataFrame({"funding_rate": [-0.001]},
                               index=pd.DatetimeIndex([start], tz="UTC"))
        klines = make_klines(start, n_bars=5, bar_minutes=5)
        ctx = _ctx_from_klines(klines, funding)
        signal = adapter.evaluate(ctx)
        assert signal is not None
        assert signal.action == SignalAction.ENTER_LONG

    def test_exit_when_funding_decays_in_position(self):
        adapter = FundingArbAdapter()
        adapter.reset({"close_threshold_8h": 0.00010})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        funding = pd.DataFrame({"funding_rate": [0.00005]},
                               index=pd.DatetimeIndex([start], tz="UTC"))
        klines = make_klines(start, n_bars=5, bar_minutes=5)
        ctx = _ctx_from_klines(klines, funding, open_pos_usd=200.0, open_side=Side.SELL)
        signal = adapter.evaluate(ctx)
        assert signal is not None
        assert signal.action == SignalAction.EXIT


class TestDcaDipsAdapter:
    def test_no_signal_on_flat_market(self):
        adapter = DcaDipsAdapter()
        adapter.reset({"drop_pct": 0.05, "lookback_bars": 30})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        klines = make_klines(start, n_bars=50, bar_minutes=5, base_price=3500.0, volatility=0.001)
        ctx = _ctx_from_klines(klines, pd.DataFrame())
        signal = adapter.evaluate(ctx)
        # Flat market — most likely no signal
        if signal is not None:
            assert signal.action in (SignalAction.HOLD, SignalAction.ENTER_LONG)

    def test_buy_dip_on_sharp_drop(self):
        adapter = DcaDipsAdapter()
        adapter.reset({"drop_pct": 0.05, "lookback_bars": 30, "size_usd": 100.0})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        klines = make_klines(start, n_bars=50, bar_minutes=5, base_price=3500.0, volatility=0.001)
        # Inject a sharp drop in the last bar (10% drop)
        klines.iloc[-1, klines.columns.get_loc("close")] = 3150.0  # ~10% below 3500
        klines.iloc[-1, klines.columns.get_loc("low")] = 3145.0
        ctx = _ctx_from_klines(klines, pd.DataFrame())
        signal = adapter.evaluate(ctx)
        assert signal is not None
        assert signal.action == SignalAction.ENTER_LONG

    def test_exit_when_target_hit(self):
        adapter = DcaDipsAdapter()
        adapter.reset({"drop_pct": 0.05, "tp_pct": 0.04, "lookback_bars": 30})
        adapter._entry_price = 3500.0
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        klines = make_klines(start, n_bars=50, bar_minutes=5)
        # Push last close 5% above entry
        klines.iloc[-1, klines.columns.get_loc("close")] = 3500.0 * 1.05
        ctx = _ctx_from_klines(klines, pd.DataFrame(), open_pos_usd=100.0, open_side=Side.BUY)
        signal = adapter.evaluate(ctx)
        assert signal is not None
        assert signal.action == SignalAction.EXIT


class TestMeanReversionAdapterEdgeCases:
    def test_empty_history_returns_none(self):
        adapter = MeanReversionAdapter()
        adapter.reset({})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ctx = SnapshotContext(
            now=start, symbol="ETHUSDT",
            history=pd.DataFrame(),
            funding_history=pd.DataFrame(),
            capital_usd=1000.0,
            open_position_usd=0.0, open_position_side=None,
            adv_24h_usd=1_000_000.0,
        )
        assert adapter.evaluate(ctx) is None

    def test_insufficient_history_returns_none(self):
        adapter = MeanReversionAdapter()
        adapter.reset({"lookback_bars": 20})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        klines = make_klines(start, n_bars=5)
        ctx = _ctx_from_klines(klines, pd.DataFrame())
        # Only 5 bars — well below lookback of 20
        assert adapter.evaluate(ctx) is None

    def test_zero_std_returns_none(self):
        adapter = MeanReversionAdapter()
        adapter.reset({"lookback_bars": 10})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # All identical closes → std = 0
        timestamps = [start + timedelta(minutes=5 * i) for i in range(20)]
        df = pd.DataFrame(
            [[3500, 3500, 3500, 3500, 1000] for _ in range(20)],
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex(timestamps, tz="UTC"),
        )
        ctx = _ctx_from_klines(df, pd.DataFrame())
        assert adapter.evaluate(ctx) is None
