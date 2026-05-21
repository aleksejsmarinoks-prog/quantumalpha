"""WalkForward harness tests (Phase 6.3.1a Step 5c-B)."""

from __future__ import annotations

import json
import math
import random
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, ReplayEngineV2, IndicatorsProvider, make_regime_provider,
    WalkForwardConfig, WalkForwardWindow, WalkForwardReport, WalkForwardHarness,
    SnapshotContext, OpenAction, ProductionAdapter,
)
from bot.backtest.adapters import MeanReversionAdapter, DcaDipsAdapter


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Bar builders
# ---------------------------------------------------------------------------

def _synth_bars(days: int, seed: int = 0, base: float = 2000.0,
                 sigma: float = 3.0) -> List[Bar]:
    """`days` of 5m bars (288 per day)."""
    rng = random.Random(seed)
    bars: List[Bar] = []
    price = base
    for i in range(days * 288):
        change = rng.gauss(0, sigma)
        next_close = max(100, price + change)
        high = max(price, next_close) + abs(change) * 0.3
        low = min(price, next_close) - abs(change) * 0.3
        bars.append(Bar(
            timestamp=T0 + timedelta(minutes=5 * i),
            open=price, high=high, low=low, close=next_close, volume=1000.0,
        ))
        price = next_close
    return bars


def _adapter_factory():
    """Standard mean_rev factory for most tests."""
    return MeanReversionAdapter(starting_capital_usd=200.0)


def _full_harness(bars, **overrides):
    """Build harness with standard plumbing — common test helper."""
    kwargs = dict(
        bars=bars,
        config=WalkForwardConfig(),
        adapter_factory=_adapter_factory,
        indicators_provider_factory=lambda b: IndicatorsProvider(b).callable_for_engine(),
        regime_provider_factory=lambda b: make_regime_provider(b),
        symbol="ETHUSDT",
        initial_equity=200.0,
    )
    kwargs.update(overrides)
    return WalkForwardHarness(**kwargs)


# ===========================================================================
# Config
# ===========================================================================

class TestConfig:

    def test_defaults_match_approved(self):
        c = WalkForwardConfig()
        assert c.train_window_days == 30
        assert c.test_window_days == 7
        assert c.step_days == 7
        assert c.walk_type == "rolling"
        assert c.fail_fast is False
        assert c.min_bars_in_test == 100

    def test_invalid_train_window_raises(self):
        with pytest.raises(ValueError, match="train_window_days"):
            WalkForwardConfig(train_window_days=0)

    def test_invalid_test_window_raises(self):
        with pytest.raises(ValueError, match="test_window_days"):
            WalkForwardConfig(test_window_days=-1)

    def test_invalid_step_raises(self):
        with pytest.raises(ValueError, match="step_days"):
            WalkForwardConfig(step_days=0)

    def test_expanding_not_yet_supported(self):
        with pytest.raises(NotImplementedError, match="expanding"):
            WalkForwardConfig(walk_type="expanding")

    def test_unknown_walk_type_raises(self):
        with pytest.raises(ValueError, match="walk_type"):
            WalkForwardConfig(walk_type="zigzag")


# ===========================================================================
# Harness construction
# ===========================================================================

class TestHarnessConstruction:

    def test_empty_bars_raises(self):
        with pytest.raises(ValueError, match="at least 1 bar"):
            WalkForwardHarness(
                bars=[], config=WalkForwardConfig(),
                adapter_factory=_adapter_factory,
            )

    def test_bars_sorted_internally(self):
        bars = _synth_bars(days=40)
        shuffled = bars[10:] + bars[:10]
        harness = WalkForwardHarness(
            bars=shuffled, config=WalkForwardConfig(),
            adapter_factory=_adapter_factory,
        )
        # Internal storage is sorted
        for i in range(len(harness._bars) - 1):
            assert harness._bars[i].timestamp <= harness._bars[i + 1].timestamp


# ===========================================================================
# Window splitting
# ===========================================================================

class TestWindowSplitting:

    def test_60_days_yields_4_windows(self):
        """60 days, rolling 30/7 step 7:
          W0: train 0-30, test 30-37
          W1: train 7-37, test 37-44
          W2: train 14-44, test 44-51
          W3: train 21-51, test 51-58
          W4 would be: train 28-58, test 58-65 → exceeds 60 days
        """
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        windows = harness._build_window_specs()
        assert len(windows) == 4

    def test_insufficient_data_yields_zero_windows(self):
        """< 37 days → no full window possible."""
        bars = _synth_bars(days=20)
        harness = _full_harness(bars)
        windows = harness._build_window_specs()
        assert windows == []

    def test_windows_are_non_overlapping_in_test(self):
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        windows = harness._build_window_specs()
        for i in range(len(windows) - 1):
            _, _, t_start, t_end = windows[i]
            _, _, next_t_start, _ = windows[i + 1]
            # Test slice end matches next window's test start (step = test_window)
            assert next_t_start == t_end

    def test_window_timestamps_aligned_with_step(self):
        """W1 train_start = W0 train_start + step_days."""
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        windows = harness._build_window_specs()
        w0_train_start = windows[0][0]
        w1_train_start = windows[1][0]
        assert w1_train_start - w0_train_start == timedelta(days=7)


# ===========================================================================
# Run — happy path
# ===========================================================================

class TestRunHappyPath:

    def test_run_60_days_succeeds(self):
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        assert isinstance(report, WalkForwardReport)
        assert len(report.windows) == 4
        # All windows succeeded (no exceptions on synthetic data)
        assert all(w.succeeded for w in report.windows)

    def test_zero_windows_when_data_too_short(self):
        bars = _synth_bars(days=20)
        harness = _full_harness(bars)
        report = harness.run()
        assert report.windows == []
        # Aggregate handles empty case
        agg = report.aggregate
        assert agg["windows_total"] == 0

    def test_engine_runs_on_test_slice_only(self):
        """Engine should process exactly test_window_days worth of bars per window."""
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        for w in report.windows:
            # 7 days × 288 bars/day = 2016 bars per window
            assert w.test_bars_count == 2016
            assert w.result.bars_processed == 2016


# ===========================================================================
# Per-window adapter freshness
# ===========================================================================

class TestAdapterFreshness:

    def test_fresh_adapter_per_window(self):
        """Each window should get a NEW adapter instance — no state leak."""
        adapters_seen = []

        def factory():
            ad = MeanReversionAdapter(starting_capital_usd=200.0)
            adapters_seen.append(id(ad))
            return ad

        bars = _synth_bars(days=60)
        harness = _full_harness(bars, adapter_factory=factory)
        report = harness.run()
        # 4 windows → 4 distinct adapter instances
        assert len(adapters_seen) == 4
        assert len(set(adapters_seen)) == 4   # all unique ids

    def test_kernel_starts_fresh_per_window(self):
        """Each window starts with fresh kernel state (zero PnL, no halt)."""
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        # Every window's adapter kernel starts at initial_equity
        for w in report.windows:
            assert w.adapter_stats is not None
            kernel_status = w.adapter_stats["risk_kernel_status"]
            # starting_equity always = initial_equity (200) — kernel never carries DD across windows
            assert kernel_status["starting_equity"] == 200.0


# ===========================================================================
# Failure handling
# ===========================================================================

class TestFailureHandling:

    def test_window_fail_continues_when_fail_fast_false(self):
        """Make the adapter factory raise occasionally; harness skips and continues."""
        call_count = [0]

        def flaky_factory():
            call_count[0] += 1
            if call_count[0] == 2:    # fail second window only
                raise RuntimeError("adapter factory boom")
            return MeanReversionAdapter(starting_capital_usd=200.0)

        bars = _synth_bars(days=60)
        harness = _full_harness(bars, adapter_factory=flaky_factory)
        report = harness.run()

        assert len(report.windows) == 4
        succ = [w for w in report.windows if w.succeeded]
        fail = [w for w in report.windows if not w.succeeded]
        assert len(succ) == 3
        assert len(fail) == 1
        assert "boom" in fail[0].error

    def test_window_fail_aborts_when_fail_fast_true(self):
        call_count = [0]

        def flaky_factory():
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("adapter factory boom")
            return MeanReversionAdapter(starting_capital_usd=200.0)

        bars = _synth_bars(days=60)
        harness = _full_harness(
            bars, adapter_factory=flaky_factory,
            config=WalkForwardConfig(fail_fast=True),
        )
        with pytest.raises(RuntimeError, match="window 1 failed"):
            harness.run()

    def test_insufficient_test_bars_skips_window(self):
        """If test slice has fewer than min_bars_in_test, window is skipped with error."""
        # Construct a gap-y bar series — test slice will have very few bars
        # Easier: bump min_bars_in_test ridiculously high
        bars = _synth_bars(days=60)
        harness = _full_harness(
            bars,
            config=WalkForwardConfig(min_bars_in_test=10000),  # impossible threshold
        )
        report = harness.run()
        # All windows fail with insufficient-bars error
        assert all(not w.succeeded for w in report.windows)
        assert all("insufficient test bars" in (w.error or "") for w in report.windows)


# ===========================================================================
# Aggregate
# ===========================================================================

class TestAggregate:

    def test_aggregate_basic_metrics(self):
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        agg = report.aggregate
        assert agg["windows_total"] == 4
        assert agg["windows_succeeded"] == 4
        assert agg["windows_failed"] == 0
        assert "return_pct_mean" in agg
        assert "max_drawdown_pct_worst" in agg

    def test_aggregate_handles_zero_successful_windows(self):
        bars = _synth_bars(days=60)

        def always_fail_factory():
            raise RuntimeError("forced fail")

        harness = _full_harness(bars, adapter_factory=always_fail_factory)
        report = harness.run()
        agg = report.aggregate
        assert agg["windows_succeeded"] == 0
        assert agg["windows_failed"] == 4
        # No return stats — fine
        assert "return_pct_mean" not in agg

    def test_profitable_vs_unprofitable_counts(self):
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        agg = report.aggregate
        total = agg["windows_profitable"] + agg["windows_unprofitable"] + agg["windows_neutral"]
        assert total == agg["windows_succeeded"]


# ===========================================================================
# JSON serialization
# ===========================================================================

class TestSerialization:

    def test_to_json_returns_valid_json(self):
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        s = report.to_json()
        parsed = json.loads(s)
        assert "symbol" in parsed
        assert "windows" in parsed
        assert "aggregate" in parsed
        assert len(parsed["windows"]) == 4

    def test_to_json_file_writes_disk(self, tmp_path):
        bars = _synth_bars(days=60)
        harness = _full_harness(bars)
        report = harness.run()
        out_path = tmp_path / "wf.json"
        report.to_json_file(out_path)
        assert out_path.exists()
        parsed = json.loads(out_path.read_text())
        assert parsed["total_bars"] == 60 * 288


# ===========================================================================
# Cross-adapter compatibility
# ===========================================================================

class TestMultiAdapter:

    def test_works_with_dca_dips_adapter(self):
        """Harness is adapter-agnostic — DcaDipsAdapter also works."""
        bars = _synth_bars(days=60)
        harness = _full_harness(
            bars,
            adapter_factory=lambda: DcaDipsAdapter(starting_capital_usd=200.0),
        )
        report = harness.run()
        assert len(report.windows) == 4
        assert all(w.succeeded for w in report.windows)


# ===========================================================================
# Smoke (visibility)
# ===========================================================================

class TestSmoke:

    def test_walk_forward_smoke_telemetry(self):
        """Full smoke run with telemetry printed for human eyes."""
        bars = _synth_bars(days=90, sigma=4.0)   # 90 days for more windows
        harness = _full_harness(bars)
        report = harness.run()

        print("\n=== Walk-forward smoke telemetry ===")
        print(f"Symbol:            {report.symbol}")
        print(f"Total bars:        {report.total_bars}")
        print(f"Windows total:     {report.aggregate['windows_total']}")
        print(f"Windows succeeded: {report.aggregate['windows_succeeded']}")
        print(f"Windows failed:    {report.aggregate['windows_failed']}")
        agg = report.aggregate
        if agg.get("return_pct_mean") is not None:
            print(f"Return mean/med:   {agg['return_pct_mean']:+.2f}% / "
                  f"{agg['return_pct_median']:+.2f}%")
            print(f"Return min/max:    {agg['return_pct_min']:+.2f}% / "
                  f"{agg['return_pct_max']:+.2f}%")
            if "return_pct_stdev" in agg:
                print(f"Return stdev:      {agg['return_pct_stdev']:.4f}%")
            print(f"Max DD worst:      {agg['max_drawdown_pct_worst']:.2f}%")
            print(f"Profitable wins:   {agg['windows_profitable']}/{agg['windows_succeeded']}")
            print(f"Trades total:      {agg['trades_total']}")
            print(f"Trades mean/win:   {agg.get('trades_mean_per_window', 0):.2f}")
        print("=====================================\n")

        # Sanity assertions
        assert math.isfinite(agg.get("return_pct_mean", 0))
