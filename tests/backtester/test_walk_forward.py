"""Tests for bot.backtester.walk_forward."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from bot.backtester.models import WindowResult
from bot.backtester.walk_forward import (
    GATE_MAX_MDD_PCT,
    GATE_MIN_MEDIAN_SHARPE,
    GATE_MIN_MIN_WINRATE,
    GATE_MIN_PROFITABLE_PCT,
    WalkForwardValidator,
    aggregate_verdict,
    generate_windows,
    grid_combinations,
)


def _dt(year=2026, month=1, day=1):
    return datetime(year, month, day, tzinfo=timezone.utc)


class TestGenerateWindows:
    def test_basic_4_windows(self):
        # 180 days, train 60, test 30, step 30 → expect 4 windows
        windows = generate_windows(_dt(2025, 11, 1), _dt(2026, 5, 1), 60, 30, 30)
        assert len(windows) == 4

    def test_insufficient_data_returns_empty(self):
        # 30 days total — can't fit 60+30
        windows = generate_windows(_dt(2026, 1, 1), _dt(2026, 1, 31), 60, 30, 30)
        assert windows == []

    def test_no_gap_between_train_and_test(self):
        windows = generate_windows(_dt(2026, 1, 1), _dt(2026, 6, 1), 60, 30, 30)
        for tr_s, tr_e, te_s, te_e in windows:
            assert tr_e == te_s

    def test_step_smaller_than_test_window_overlaps(self):
        windows = generate_windows(_dt(2026, 1, 1), _dt(2026, 6, 1), 60, 30, 15)
        # Adjacent test windows should overlap
        if len(windows) >= 2:
            assert windows[1][2] < windows[0][3]                    # test_start[1] < test_end[0]

    def test_exact_fit_boundary(self):
        # 90 days fits exactly one window
        windows = generate_windows(_dt(2026, 1, 1), _dt(2026, 4, 1), 60, 30, 30)
        assert len(windows) == 1


class TestGridCombinations:
    def test_empty_grid(self):
        assert grid_combinations({}) == [{}]

    def test_single_param(self):
        combos = grid_combinations({"x": [1, 2, 3]})
        assert len(combos) == 3
        assert {"x": 1} in combos

    def test_two_params(self):
        combos = grid_combinations({"a": [1, 2], "b": [10, 20, 30]})
        assert len(combos) == 6
        assert {"a": 1, "b": 10} in combos
        assert {"a": 2, "b": 30} in combos


class TestAggregateVerdict:
    def _window(self, idx: int, test_sharpe: float, test_mdd: float, test_wr: float, test_pnl: float) -> WindowResult:
        return WindowResult(
            window_idx=idx,
            train_start=_dt(2026, 1, 1), train_end=_dt(2026, 3, 1),
            test_start=_dt(2026, 3, 1), test_end=_dt(2026, 4, 1),
            best_params={"x": idx},
            train_metrics={"sharpe_annualized": 1.5},
            test_metrics={
                "sharpe_annualized": test_sharpe,
                "max_drawdown_pct": test_mdd,
                "win_rate": test_wr,
                "total_pnl_usd": test_pnl,
            },
            train_trades=10, test_trades=5,
        )

    def test_empty_results(self):
        v = aggregate_verdict("test_strat", [])
        assert v["passes_all"] is False
        assert "FAIL" in v["verdict_text"]

    def test_all_gates_pass(self):
        windows = [
            self._window(0, 1.5, 0.05, 0.50, 100.0),
            self._window(1, 1.3, 0.04, 0.45, 80.0),
            self._window(2, 1.1, 0.06, 0.42, 50.0),
            self._window(3, 1.4, 0.03, 0.55, 120.0),
        ]
        v = aggregate_verdict("good_strat", windows)
        assert v["passes_sharpe"] is True
        assert v["passes_mdd"] is True
        assert v["passes_winrate"] is True
        assert v["passes_profitable_pct"] is True
        assert v["passes_all"] is True
        assert "PASS" in v["verdict_text"]

    def test_fails_on_sharpe(self):
        windows = [
            self._window(0, 0.5, 0.05, 0.50, 100.0),
            self._window(1, 0.3, 0.04, 0.45, 80.0),
        ]
        v = aggregate_verdict("bad", windows)
        assert v["passes_sharpe"] is False
        assert v["passes_all"] is False

    def test_fails_on_mdd(self):
        windows = [self._window(0, 1.5, 0.15, 0.50, 100.0)]
        v = aggregate_verdict("dangerous", windows)
        assert v["passes_mdd"] is False
        assert v["passes_all"] is False


class TestWalkForwardValidatorMechanics:
    def test_runner_called_for_each_combo(self):
        # Synthetic runner that records every (params, start, end) call
        calls = []

        def runner(params, start, end):
            calls.append((params, start, end))
            curve = pd.Series([100, 102, 104], index=pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC"))
            return [], curve

        validator = WalkForwardValidator(
            runner=runner,
            param_grid={"x": [1, 2, 3]},
            train_days=60, test_days=30, step_days=30,
        )
        results = validator.run(_dt(2026, 1, 1), _dt(2026, 6, 1))
        # Each window: 3 combos for train + 1 for test = 4 calls
        assert len(calls) >= 4 * len(results)
        assert all(isinstance(r, WindowResult) for r in results)
