"""
QA Backtester — Walk-Forward Validation
========================================

Rolling train/test windows for out-of-sample validation. Train window
optimises parameters (grid search → best Sharpe), test window measures
unseen performance.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Protocol

import pandas as pd

from .metrics import compute_metrics
from .models import Trade, WindowResult


log = logging.getLogger("qa.backtester.walk_forward")


# ─────────────────────────────────────────────────────────────────────────────
# Window math
# ─────────────────────────────────────────────────────────────────────────────

def generate_windows(
    data_start: datetime,
    data_end: datetime,
    train_days: int = 60,
    test_days: int = 30,
    step_days: int = 30,
) -> list[tuple[datetime, datetime, datetime, datetime]]:
    """
    Return list of (train_start, train_end, test_start, test_end) tuples.

    Each window: train_end == test_start (no gap). Successive windows shift
    by `step_days`. Windows extending past data_end are excluded.
    """
    windows: list[tuple[datetime, datetime, datetime, datetime]] = []
    cursor = data_start
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > data_end:
            break
        windows.append((train_start, train_end, test_start, test_end))
        cursor = cursor + timedelta(days=step_days)
    return windows


def grid_combinations(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Expand a param grid into all combinations."""
    if not grid:
        return [{}]
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    combos = []
    for combo_values in itertools.product(*values):
        combos.append(dict(zip(keys, combo_values)))
    return combos


# ─────────────────────────────────────────────────────────────────────────────
# Backtest runner Protocol — abstract over how a backtest is actually executed
# ─────────────────────────────────────────────────────────────────────────────

class BacktestRunnerProto(Protocol):
    """
    A callable that runs ONE backtest over [start, end] with given params
    and returns (trades, equity_curve).
    """

    def __call__(
        self,
        params: dict,
        start: datetime,
        end: datetime,
    ) -> tuple[list[Trade], pd.Series]: ...


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

class WalkForwardValidator:
    """
    Run grid-search optimisation on each train window, evaluate best params
    out-of-sample on subsequent test window.

    The actual backtest is delegated to a `runner` callable to keep this
    module decoupled from ReplayEngine internals.
    """

    def __init__(
        self,
        runner: BacktestRunnerProto,
        param_grid: dict[str, list[Any]],
        train_days: int = 60,
        test_days: int = 30,
        step_days: int = 30,
        optimisation_metric: str = "sharpe_annualized",
    ):
        self.runner = runner
        self.param_grid = param_grid
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.optim_metric = optimisation_metric

    def run(self, data_start: datetime, data_end: datetime) -> list[WindowResult]:
        windows = generate_windows(
            data_start, data_end,
            train_days=self.train_days,
            test_days=self.test_days,
            step_days=self.step_days,
        )
        log.info("walk-forward: %d windows generated", len(windows))
        if not windows:
            log.warning("no windows fit between %s and %s", data_start.date(), data_end.date())
            return []

        results: list[WindowResult] = []
        for idx, (tr_s, tr_e, te_s, te_e) in enumerate(windows):
            log.info("window %d/%d: train %s..%s test %s..%s",
                     idx + 1, len(windows), tr_s.date(), tr_e.date(), te_s.date(), te_e.date())
            best_params, best_train_metrics, best_train_trades = self._optimise_on_train(tr_s, tr_e)
            test_trades, test_equity = self.runner(best_params, te_s, te_e)
            test_metrics = compute_metrics(test_trades, test_equity)

            results.append(WindowResult(
                window_idx=idx,
                train_start=tr_s,
                train_end=tr_e,
                test_start=te_s,
                test_end=te_e,
                best_params=best_params,
                train_metrics=best_train_metrics,
                test_metrics=test_metrics,
                train_trades=best_train_trades,
                test_trades=len([t for t in test_trades if t.is_closed]),
            ))
        return results

    def _optimise_on_train(
        self, train_start: datetime, train_end: datetime,
    ) -> tuple[dict, dict, int]:
        """Grid-search across param_grid, return (best_params, metrics, n_trades)."""
        combos = grid_combinations(self.param_grid)
        if not combos:
            combos = [{}]
        best_score = float("-inf")
        best_params: dict = combos[0]
        best_metrics: dict = {}
        best_n_trades = 0
        for params in combos:
            trades, equity = self.runner(params, train_start, train_end)
            metrics = compute_metrics(trades, equity)
            score = metrics.get(self.optim_metric, 0.0)
            if score > best_score:
                best_score = score
                best_params = params
                best_metrics = metrics
                best_n_trades = len([t for t in trades if t.is_closed])
        return best_params, best_metrics, best_n_trades


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate verdict against acceptance gates
# ─────────────────────────────────────────────────────────────────────────────

# Phase 6.3 spec gates:
GATE_MIN_MEDIAN_SHARPE = 1.0
GATE_MAX_MDD_PCT = 0.08
GATE_MIN_MIN_WINRATE = 0.38
GATE_MIN_PROFITABLE_PCT = 0.60


def aggregate_verdict(strategy_name: str, results: list[WindowResult]) -> dict:
    """
    Apply Phase 6.3 acceptance gates to walk-forward results. Returns a dict
    suitable for embedding in the JSON report and the Markdown summary.
    """
    if not results:
        return {
            "strategy_name": strategy_name,
            "median_test_sharpe": 0.0,
            "max_test_mdd_pct": 0.0,
            "min_test_winrate": 0.0,
            "pct_profitable_windows": 0.0,
            "passes_sharpe": False,
            "passes_mdd": False,
            "passes_winrate": False,
            "passes_profitable_pct": False,
            "passes_all": False,
            "verdict_text": "FAIL — no windows produced",
        }

    test_sharpes = [w.test_metrics.get("sharpe_annualized", 0.0) for w in results]
    test_mdds = [w.test_metrics.get("max_drawdown_pct", 1.0) for w in results]
    test_wins = [w.test_metrics.get("win_rate", 0.0) for w in results]
    pnl_signs = [1 if w.test_metrics.get("total_pnl_usd", 0.0) > 0 else 0 for w in results]

    median_sharpe = float(pd.Series(test_sharpes).median())
    max_mdd = max(test_mdds)
    min_wr = min(test_wins)
    profitable_pct = sum(pnl_signs) / len(pnl_signs)

    passes_sharpe = median_sharpe >= GATE_MIN_MEDIAN_SHARPE
    passes_mdd = max_mdd <= GATE_MAX_MDD_PCT
    passes_winrate = min_wr >= GATE_MIN_MIN_WINRATE
    passes_profitable_pct = profitable_pct >= GATE_MIN_PROFITABLE_PCT
    passes_all = all([passes_sharpe, passes_mdd, passes_winrate, passes_profitable_pct])

    return {
        "strategy_name": strategy_name,
        "median_test_sharpe": median_sharpe,
        "max_test_mdd_pct": max_mdd,
        "min_test_winrate": min_wr,
        "pct_profitable_windows": profitable_pct,
        "passes_sharpe": passes_sharpe,
        "passes_mdd": passes_mdd,
        "passes_winrate": passes_winrate,
        "passes_profitable_pct": passes_profitable_pct,
        "passes_all": passes_all,
        "verdict_text": "PASS — ready for live consideration" if passes_all else "FAIL — do not deploy to live",
    }
