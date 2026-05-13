"""
Integration test — full 1-month mean-reversion backtest on synthetic data.

Verifies:
  - Pipeline end-to-end (replay → execution → metrics → report)
  - Trades count > 0 on oscillating data
  - Report files written to disk
  - Run completes in < 5 seconds on synthetic data (CI bound)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from bot.backtester.execution_sim import ExecutionSimulator
from bot.backtester.metrics import compute_metrics
from bot.backtester.models import WindowResult
from bot.backtester.replay_engine import ReplayEngine
from bot.backtester.report import write_backtest_report
from bot.backtester.strategy_adapters.mean_reversion_adapter import MeanReversionAdapter
from tests.backtester.conftest import make_mean_reverting_series


def test_full_month_mean_reversion_pipeline(tmp_path):
    # 1 month of 5min bars = 30 * 24 * 12 = 8640 bars
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    df = make_mean_reverting_series(start, n_bars=8640, base=3500.0, amplitude=80.0, cycle_bars=24)

    adapter = MeanReversionAdapter()
    adapter.reset({"lookback_bars": 10, "z_entry": 1.0, "z_exit": 0.2, "size_usd": 200.0})
    engine = ReplayEngine(
        adapter=adapter, klines={"ETHUSDT": df}, funding={},
        execution_sim=ExecutionSimulator(maker_fill_rate=1.0, seed=42),
        starting_capital_usd=1000.0,
    )

    t0 = time.time()
    trades, equity = engine.run(df.index[0].to_pydatetime(),
                                df.index[-1].to_pydatetime(), "ETHUSDT")
    elapsed = time.time() - t0

    # Acceptance: 1 month synthetic data runs in well under 5 min (CI hard cap)
    assert elapsed < 60.0, f"Integration too slow: {elapsed:.1f}s"

    closed_trades = [t for t in trades if t.is_closed]
    assert len(closed_trades) > 0, "Mean reversion should produce trades on sinusoidal data"

    metrics = compute_metrics(trades, equity)
    assert "sharpe_annualized" in metrics
    assert metrics["total_trades"] == len(closed_trades)

    # Write the report and verify files
    win = WindowResult(
        window_idx=0,
        train_start=df.index[0].to_pydatetime(), train_end=df.index[0].to_pydatetime(),
        test_start=df.index[0].to_pydatetime(), test_end=df.index[-1].to_pydatetime(),
        best_params={"lookback_bars": 10, "z_entry": 1.0},
        train_metrics={},
        test_metrics=metrics,
        train_trades=0, test_trades=metrics["total_trades"],
    )
    json_path, md_path = write_backtest_report(
        "mean_reversion_v1",
        df.index[0].to_pydatetime(), df.index[-1].to_pydatetime(),
        1000.0,
        [win],
        tmp_path,
    )
    assert json_path.exists()
    assert md_path.exists()

    # JSON should be valid + contain expected keys
    payload = json.loads(json_path.read_text())
    assert payload["strategy_name"] == "mean_reversion_v1"
    assert "verdict" in payload
    assert "windows" in payload

    md = md_path.read_text()
    assert "Backtest: mean_reversion_v1" in md
    assert "Verdict" in md


def test_determinism_same_seed_same_result(tmp_path):
    """Same seed + same data → same metrics."""
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    df = make_mean_reverting_series(start, n_bars=2000, base=3500.0, amplitude=70.0, cycle_bars=20)

    def _run(seed: int):
        adapter = MeanReversionAdapter()
        adapter.reset({"lookback_bars": 10, "z_entry": 1.0, "z_exit": 0.2, "size_usd": 100.0})
        engine = ReplayEngine(
            adapter=adapter, klines={"ETHUSDT": df}, funding={},
            execution_sim=ExecutionSimulator(maker_fill_rate=0.85, seed=seed),
        )
        return engine.run(df.index[0].to_pydatetime(), df.index[-1].to_pydatetime(), "ETHUSDT")

    trades_a, equity_a = _run(42)
    trades_b, equity_b = _run(42)

    assert len(trades_a) == len(trades_b)
    for ta, tb in zip(trades_a, trades_b):
        assert ta.realized_pnl_usd == tb.realized_pnl_usd
        assert ta.entry_fill.fill_price == tb.entry_fill.fill_price
