"""ReplayEngine v2 tests (Phase 6.3.1a Step 1).

Covers the 9 cases from REPLAY_ENGINE_V2_DESIGN.md plus extras for
risk-kernel hook and equity tracking. Total: 14 cases.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from bot.backtest import (
    Bar, ReplayEngineV2, OpenAction, ScaleInAction, ReduceAction,
    CloseAction, TakeProfit, SnapshotContext,
)

from .conftest import OpenAtBarAdapter, ScaleInAdapter


# ---------------------------------------------------------------------------
# Case 1: Single OPEN action creates position
# ---------------------------------------------------------------------------

class TestOpenAction:

    def test_open_creates_position(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(flat_bars, adapter)

        # Position opened at bar 1 (next-bar fill after bar 0 signal)
        assert result.bars_processed == 5
        # End of data → engine closes position → trade recorded
        assert result.trade_count == 1
        assert result.open_positions == []
        # Side recorded correctly
        assert result.trades[0].side == "LONG"
        # qty
        assert result.trades[0].qty_total == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Case 2: CLOSE action records a trade
# ---------------------------------------------------------------------------

class TestCloseAction:

    def test_explicit_close_records_trade(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1, close_at_index=2)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(flat_bars, adapter)

        assert result.trade_count == 1
        # close_reason should be "adapter" (explicit CloseAction)
        assert result.trades[0].close_reason == "adapter"
        # Position not in open_positions
        assert result.open_positions == []


# ---------------------------------------------------------------------------
# Case 3: SCALE_IN — adds fill, updates avg_entry_price
# ---------------------------------------------------------------------------

class TestScaleIn:

    def test_scale_in_increases_size(self, trending_up_bars):
        adapter = ScaleInAdapter()
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        result = eng.run(trending_up_bars, adapter)

        # 2 fills expected: open (0.1) + scale_in (0.05) = total 0.15
        assert len(adapter.fills_received) == 2
        assert adapter.fills_received == [0.1, 0.05]
        # Trade qty_total == sum of fills
        assert result.trade_count == 1
        assert result.trades[0].qty_total == pytest.approx(0.15)

    def test_scale_in_updates_avg_entry_price(self, trending_up_bars):
        """First fill at bar 1 open (~2010), second fill at bar 3 open (~2030)."""
        adapter = ScaleInAdapter()
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        eng.run(trending_up_bars, adapter)

        # Bars are 2000 → 2100 stepped by +10/bar, so bar 1 open = 2010, bar 3 open = 2030
        # Avg entry of (0.1@2010 + 0.05@2030) / 0.15 = (201 + 101.5)/0.15 = 2016.67
        # We can't directly verify entry price post-close (trade.avg_entry_price)
        # but it should be > 2010 (first fill) and < 2030 (second fill)
        trade = eng.trades[0]
        assert 2010 < trade.avg_entry_price < 2030


# ---------------------------------------------------------------------------
# Case 4: REDUCE — partial close
# ---------------------------------------------------------------------------

class TestReduce:

    def test_reduce_partially_closes(self, flat_bars):
        class ReduceAdapter:
            def __init__(self):
                self._count = 0

            def evaluate(self, snapshot, positions):
                actions = []
                if self._count == 0:
                    actions.append(OpenAction(symbol=snapshot.symbol,
                                              side="LONG", qty=0.2))
                elif self._count == 2 and positions:
                    actions.append(ReduceAction(position_id=positions[0].id, qty=0.1))
                self._count += 1
                return actions

        adapter = ReduceAdapter()
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(flat_bars, adapter)

        # After REDUCE, position still has 0.1 left → end_of_data closes it
        # That gives 1 trade total (the original position)
        assert result.trade_count == 1
        # Total qty in fills was 0.2 (single OPEN, no scale_in)
        assert result.trades[0].qty_total == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Case 5: Multiple concurrent positions
# ---------------------------------------------------------------------------

class TestConcurrentPositions:

    def test_multiple_open_positions(self, trending_up_bars):
        """Adapter opens 2 positions at bar 0 and 1 respectively."""
        class MultiOpenAdapter:
            def __init__(self):
                self._count = 0

            def evaluate(self, snapshot, positions):
                actions = []
                if self._count in (0, 1):
                    actions.append(OpenAction(symbol=snapshot.symbol,
                                              side="LONG", qty=0.05,
                                              tag=f"pos{self._count}"))
                self._count += 1
                return actions

        adapter = MultiOpenAdapter()
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(trending_up_bars, adapter)

        # 2 positions opened, both closed at end_of_data → 2 trades
        assert result.trade_count == 2
        # Different position IDs
        ids = {t.position_id for t in result.trades}
        assert len(ids) == 2


# ---------------------------------------------------------------------------
# Case 6: Stop-loss triggers close
# ---------------------------------------------------------------------------

class TestStopLoss:

    def test_stop_loss_closes_position(self, crash_bars):
        """Position opens at bar 1 (~2000) with SL at 1950. Bar 2 crashes to 1900 → SL hits."""
        adapter = OpenAtBarAdapter(
            open_at_index=0, qty=0.1, side="LONG", stop_loss=1950.0,
        )
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        result = eng.run(crash_bars, adapter)

        assert result.trade_count == 1
        assert result.trades[0].close_reason == "stop_loss"
        # Exit at SL price exactly
        assert result.trades[0].avg_exit_price == pytest.approx(1950.0)
        # PnL should be negative (long entry ~2000, stop at 1950)
        assert result.trades[0].realized_pnl < 0


# ---------------------------------------------------------------------------
# Case 7: Take-profit triggers close
# ---------------------------------------------------------------------------

class TestTakeProfit:

    def test_take_profit_full_close(self, spike_up_bars):
        """LONG with TP at 2050, bar 2 hits high 2100 → TP triggers, full close."""
        adapter = OpenAtBarAdapter(
            open_at_index=0, qty=0.1, side="LONG",
            take_profits=[TakeProfit(price=2050.0, fraction=1.0)],
        )
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        result = eng.run(spike_up_bars, adapter)

        assert result.trade_count == 1
        assert result.trades[0].close_reason == "take_profit"
        assert result.trades[0].avg_exit_price == pytest.approx(2050.0)
        # Profitable trade
        assert result.trades[0].realized_pnl > 0

    def test_take_profit_partial_then_full(self, spike_up_bars):
        """Ladder: 50% at 2050, 50% at 2080. Bar 2 high = 2100 hits both."""
        adapter = OpenAtBarAdapter(
            open_at_index=0, qty=0.2, side="LONG",
            take_profits=[
                TakeProfit(price=2050.0, fraction=0.5),
                TakeProfit(price=2080.0, fraction=1.0),  # 1.0 of remaining
            ],
        )
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        result = eng.run(spike_up_bars, adapter)

        # Position fully closed → 1 trade
        assert result.trade_count == 1
        assert result.trades[0].close_reason == "take_profit"
        # Profit > 0
        assert result.trades[0].realized_pnl > 0


# ---------------------------------------------------------------------------
# Case 8: on_fill callback fires
# ---------------------------------------------------------------------------

class TestOnFillCallback:

    def test_on_fill_called(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        eng.run(flat_bars, adapter)

        assert len(adapter.fills_received) == 1
        pos_id, price, qty = adapter.fills_received[0]
        assert qty == pytest.approx(0.1)
        assert price > 0


# ---------------------------------------------------------------------------
# Case 9: on_position_closed callback fires
# ---------------------------------------------------------------------------

class TestOnPositionClosedCallback:

    def test_on_close_called(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1, close_at_index=2)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        eng.run(flat_bars, adapter)

        assert len(adapter.closes_received) == 1
        pos_id, reason, pnl = adapter.closes_received[0]
        assert reason == "adapter"


# ---------------------------------------------------------------------------
# Case 10: SnapshotContext exposes regime + macro_events + indicators
# ---------------------------------------------------------------------------

class TestSnapshotContext:

    def test_regime_passed_to_adapter(self, flat_bars, utc_now):
        """Engine builds SnapshotContext.regime from regime_provider."""
        adapter = OpenAtBarAdapter(open_at_index=99)   # never actually opens
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=1000.0,
            regime_provider=lambda ts: "LOW_VOL",
        )
        eng.run(flat_bars, adapter)

        # All snapshots should have regime="LOW_VOL"
        assert all(s.regime == "LOW_VOL" for s in adapter.snapshots_seen)

    def test_macro_events_passed(self, flat_bars, utc_now):
        fomc_event = {"time_utc": (utc_now + timedelta(hours=8)).isoformat(),
                      "name": "FOMC", "importance": "high"}
        adapter = OpenAtBarAdapter(open_at_index=99)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=1000.0,
            calendar_provider=lambda ts: [fomc_event],
        )
        eng.run(flat_bars, adapter)

        # Every snapshot has FOMC in macro_events
        assert all(len(s.macro_events) == 1 for s in adapter.snapshots_seen)
        assert adapter.snapshots_seen[0].macro_events[0]["name"] == "FOMC"

    def test_indicators_provider(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=99)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=1000.0,
            indicators_provider=lambda ts, bar: {"rsi": 50.0, "atr": 5.0},
        )
        eng.run(flat_bars, adapter)
        assert adapter.snapshots_seen[0].indicators == {"rsi": 50.0, "atr": 5.0}

    def test_provider_exception_doesnt_break_engine(self, flat_bars):
        """If regime_provider raises, engine continues with regime=None."""
        def bad_provider(ts):
            raise RuntimeError("regime service down")

        adapter = OpenAtBarAdapter(open_at_index=99)
        eng = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=1000.0,
            regime_provider=bad_provider,
        )
        # Should not raise
        eng.run(flat_bars, adapter)
        assert all(s.regime is None for s in adapter.snapshots_seen)


# ---------------------------------------------------------------------------
# Case 11: Risk kernel hook (Step 4 will provide real kernel; Step 1 ships hook)
# ---------------------------------------------------------------------------

class TestRiskKernelHook:

    def test_kernel_veto_blocks_action(self, flat_bars):
        """Risk kernel that rejects all OpenActions → no trades."""
        class VetoKernel:
            def allow_action(self, action, positions, equity, snapshot):
                return not isinstance(action, OpenAction)

        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             risk_kernel=VetoKernel())
        result = eng.run(flat_bars, adapter)

        assert result.trade_count == 0
        assert len(result.rejections) == 1
        assert result.rejections[0]["action"] == "OpenAction"

    def test_kernel_allow_lets_through(self, flat_bars):
        """Permissive kernel — actions execute normally."""
        class AllowKernel:
            def allow_action(self, action, positions, equity, snapshot):
                return True

        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             risk_kernel=AllowKernel())
        result = eng.run(flat_bars, adapter)

        assert result.trade_count == 1
        assert result.rejections == []

    def test_kernel_exception_is_veto(self, flat_bars):
        """Defensive: kernel exception treated as veto."""
        class BrokenKernel:
            def allow_action(self, *args, **kwargs):
                raise RuntimeError("kernel internal error")

        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             risk_kernel=BrokenKernel())
        result = eng.run(flat_bars, adapter)

        assert result.trade_count == 0
        assert len(result.rejections) == 1
        assert "kernel_error" in result.rejections[0]["reason"]


# ---------------------------------------------------------------------------
# Case 12: Equity curve tracks correctly
# ---------------------------------------------------------------------------

class TestEquityCurve:

    def test_equity_curve_has_one_point_per_bar(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=99)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(flat_bars, adapter)

        assert len(result.equity_curve) == len(flat_bars)

    def test_equity_reflects_pnl(self, spike_up_bars):
        """Long position into spike → equity should rise."""
        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1, side="LONG")
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        result = eng.run(spike_up_bars, adapter)

        # Bar 2 closes at 2050 (after spike to 2100). Unrealized PnL positive.
        equity_at_bar2 = result.equity_curve[2][1]
        assert equity_at_bar2 > 1000.0


# ---------------------------------------------------------------------------
# Case 13: Anti-lookahead — bars must be strictly increasing
# ---------------------------------------------------------------------------

class TestAntiLookahead:

    def test_rejects_non_monotonic_bars(self, utc_now):
        bars = [
            Bar(timestamp=utc_now + timedelta(minutes=5),
                open=2000, high=2001, low=1999, close=2000, volume=1000),
            Bar(timestamp=utc_now,    # OLDER — must raise
                open=2000, high=2001, low=1999, close=2000, volume=1000),
        ]
        adapter = OpenAtBarAdapter(open_at_index=99)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        with pytest.raises(ValueError, match="strictly increasing"):
            eng.run(bars, adapter)


# ---------------------------------------------------------------------------
# Case 14: BacktestResult.summary() returns expected keys
# ---------------------------------------------------------------------------

class TestBacktestResultSummary:

    def test_summary_keys(self, flat_bars):
        adapter = OpenAtBarAdapter(open_at_index=0, qty=0.1, close_at_index=2)
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(flat_bars, adapter)
        summary = result.summary()

        required = {
            "initial_equity", "final_equity", "total_return_pct",
            "trade_count", "win_rate", "max_drawdown_pct",
            "open_positions_at_end", "bars_processed",
            "risk_kernel_rejections",
        }
        assert required.issubset(summary.keys())
        assert summary["bars_processed"] == 5
        assert summary["trade_count"] == 1
