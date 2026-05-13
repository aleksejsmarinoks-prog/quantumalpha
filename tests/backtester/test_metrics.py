"""Tests for bot.backtester.metrics."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot.backtester.metrics import (
    avg_loss_usd,
    avg_win_usd,
    calmar_ratio,
    compute_metrics,
    largest_loss_usd,
    longest_drawdown_days,
    max_drawdown_pct,
    profit_factor,
    sharpe_annualized,
    total_pnl_usd,
    win_rate,
)
from bot.backtester.models import OrderType, Side, Trade
from tests.backtester.conftest import make_fill


def _make_closed_trade(pnl: float, side: Side = Side.BUY) -> Trade:
    entry = make_fill(side=side, fill_price=100.0, fee_usd=0.02)
    # We construct a trade and manually set pnl to avoid replicating fill math here
    exit_ = make_fill(
        timestamp=entry.timestamp + timedelta(minutes=30),
        side=Side.SELL if side == Side.BUY else Side.BUY,
        fill_price=100.0,                                  # arbitrary; PnL is overridden below
        fee_usd=0.02,
    )
    t = Trade(symbol="ETHUSDT", side=side, entry_fill=entry, exit_fill=exit_)
    t.realized_pnl_usd = pnl
    t.is_closed = True
    return t


class TestWinRate:
    def test_empty(self):
        assert win_rate([]) == 0.0

    def test_all_wins(self):
        trades = [_make_closed_trade(10.0) for _ in range(5)]
        assert win_rate(trades) == 1.0

    def test_mixed(self):
        trades = [_make_closed_trade(10.0)] * 3 + [_make_closed_trade(-5.0)] * 2
        assert win_rate(trades) == pytest.approx(3 / 5)

    def test_open_trade_ignored(self):
        open_t = _make_closed_trade(10.0)
        open_t.is_closed = False
        closed = _make_closed_trade(-5.0)
        assert win_rate([open_t, closed]) == 0.0


class TestProfitFactor:
    def test_empty(self):
        assert profit_factor([]) == 0.0

    def test_all_wins_inf(self):
        trades = [_make_closed_trade(10.0)] * 3
        assert math.isinf(profit_factor(trades))

    def test_ratio(self):
        trades = [_make_closed_trade(30.0), _make_closed_trade(-10.0)]
        assert profit_factor(trades) == 3.0


class TestPnLAggregations:
    def test_total_pnl(self):
        trades = [_make_closed_trade(10.0), _make_closed_trade(-3.0), _make_closed_trade(5.0)]
        assert total_pnl_usd(trades) == 12.0

    def test_avg_win_loss(self):
        trades = [_make_closed_trade(10.0), _make_closed_trade(20.0),
                  _make_closed_trade(-5.0), _make_closed_trade(-15.0)]
        assert avg_win_usd(trades) == 15.0
        assert avg_loss_usd(trades) == -10.0
        assert largest_loss_usd(trades) == -15.0


class TestEquityCurveMetrics:
    def _curve(self, values: list[float]) -> pd.Series:
        idx = pd.date_range("2026-01-01", periods=len(values), freq="D", tz="UTC")
        return pd.Series(values, index=idx)

    def test_max_drawdown_zero_when_monotonic(self):
        assert max_drawdown_pct(self._curve([100, 110, 120, 130])) == 0.0

    def test_max_drawdown_percent(self):
        # peak 200 → trough 150 = 25%
        dd = max_drawdown_pct(self._curve([100, 200, 150, 180]))
        assert dd == pytest.approx(0.25)

    def test_max_drawdown_empty(self):
        assert max_drawdown_pct(pd.Series(dtype=float)) == 0.0

    def test_sharpe_positive(self):
        curve = self._curve([100, 102, 104, 106, 108, 110])
        s = sharpe_annualized(curve)
        assert s > 0

    def test_sharpe_zero_std(self):
        curve = self._curve([100, 100, 100, 100])
        assert sharpe_annualized(curve) == 0.0

    def test_sharpe_empty(self):
        assert sharpe_annualized(pd.Series(dtype=float)) == 0.0

    def test_calmar_positive(self):
        curve = self._curve([100, 90, 110, 105, 115])
        c = calmar_ratio(curve)
        assert c != 0.0

    def test_calmar_no_drawdown_returns_zero(self):
        curve = self._curve([100, 110, 120])
        assert calmar_ratio(curve) == 0.0

    def test_longest_drawdown_days_zero_when_monotonic(self):
        curve = self._curve([100, 110, 120, 130])
        assert longest_drawdown_days(curve) == 0

    def test_longest_drawdown_days_counts(self):
        # 100, 120 (peak), 110 (DD), 115 (DD), 125 (recovery)
        # DD spans days 2 and 3, recovery at day 4. Duration ≈ 2 days.
        idx = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
        curve = pd.Series([100, 120, 110, 115, 125], index=idx)
        assert longest_drawdown_days(curve) >= 1


class TestComputeMetricsTopLevel:
    def test_keys_present(self):
        trades = [_make_closed_trade(10.0), _make_closed_trade(-3.0)]
        curve = pd.Series([1000, 1010, 1007, 1015], index=pd.date_range("2026-01-01", periods=4, freq="D", tz="UTC"))
        m = compute_metrics(trades, curve)
        expected_keys = {
            "total_trades", "total_pnl_usd", "win_rate", "profit_factor",
            "sharpe_annualized", "max_drawdown_pct", "calmar_ratio",
            "avg_win_usd", "avg_loss_usd", "largest_loss_usd", "longest_drawdown_days",
        }
        assert set(m.keys()) >= expected_keys
        assert m["total_trades"] == 2
        assert m["total_pnl_usd"] == 7.0

    def test_empty_trades_no_crash(self):
        m = compute_metrics([], pd.Series(dtype=float))
        assert m["total_trades"] == 0
        assert m["win_rate"] == 0.0
        assert m["sharpe_annualized"] == 0.0
