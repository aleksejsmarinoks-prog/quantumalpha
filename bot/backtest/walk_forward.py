"""
QA Backtest — Walk-Forward Harness (Phase 6.3.1a Step 5c-B)
=============================================================

Orchestrates multi-window rolling walk-forward backtests using
ReplayEngineV2 (Step 1) + ProductionAdapter subclasses (Step 4+5).

Design defaults (approved by Aleksejs 18 May 2026):
  - Rolling 30-day train / 7-day test windows
  - Step 7 days (non-overlapping test slices)
  - Single-symbol per harness (multi-symbol = orchestrate externally)
  - Window failure: skip + log + continue (fail_fast=False default)
  - Adapter rebuilt fresh per window via factory (clean state, no leak)
  - Kernel reset per window (matches production semantics)
  - Output: WalkForwardReport with per-window + aggregate stats

Anti-lookahead
--------------
Providers (regime + indicators) are constructed with the FULL bar history
(train + test) so they have proper warmup. They internally use anti-lookahead
(only bars strictly before query timestamp). Engine itself runs ONLY on test
slice bars — train slice is warmup-only for providers.

Walk types
----------
  - "rolling" (default, this ship): train window slides forward
  - "expanding" (documented, not yet shipped): train starts at data beginning,
     extends forward each window. Future enhancement.

Usage
-----
    from bot.backtest import (
        ReplayEngineV2, IndicatorsProvider, make_regime_provider,
        WalkForwardHarness, WalkForwardConfig,
    )
    from bot.backtest.adapters import MeanReversionAdapter

    adapter_factory = lambda: MeanReversionAdapter(starting_capital_usd=200.0)
    indicators_factory = lambda bars: IndicatorsProvider(bars).callable_for_engine()
    regime_factory = lambda bars: make_regime_provider(bars)

    harness = WalkForwardHarness(
        bars=all_bars,
        config=WalkForwardConfig(),
        adapter_factory=adapter_factory,
        indicators_provider_factory=indicators_factory,
        regime_provider_factory=regime_factory,
        symbol="ETHUSDT",
        initial_equity=200.0,
    )
    report = harness.run()
    print(report.summary())
    report.to_json_file("/tmp/wf_report.json")

Author: QuantumAlpha
Phase: 6.3.1a Step 5c-B
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .models import Bar
from .replay_engine_v2 import BacktestResult, ReplayEngineV2
from .production_adapter_base import ProductionAdapter

logger = logging.getLogger("qa.backtest.walk_forward")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkForwardConfig:
    """Walk-forward harness configuration.

    Approved defaults (Aleksejs 18 May 2026): rolling 30/7, step 7,
    sequential single-symbol, skip-on-window-fail, fresh adapter per window.
    """
    train_window_days: int = 30
    test_window_days: int = 7
    step_days: int = 7
    walk_type: str = "rolling"      # "rolling" (shipped) | "expanding" (future)
    min_bars_in_test: int = 100     # skip windows with fewer test bars (data gaps)
    fail_fast: bool = False         # default skip-and-continue
    engine_kwargs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.train_window_days <= 0:
            raise ValueError("train_window_days must be > 0")
        if self.test_window_days <= 0:
            raise ValueError("test_window_days must be > 0")
        if self.step_days <= 0:
            raise ValueError("step_days must be > 0")
        if self.walk_type not in ("rolling", "expanding"):
            raise ValueError(f"walk_type must be 'rolling' or 'expanding', got {self.walk_type!r}")
        if self.walk_type == "expanding":
            raise NotImplementedError("expanding walk-forward planned for future enhancement; use 'rolling' for now")


# ---------------------------------------------------------------------------
# Window result
# ---------------------------------------------------------------------------

@dataclass
class WalkForwardWindow:
    """Single window's result. Either successful (result populated, error=None)
    or failed (error set, result=None)."""
    window_id: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_bars_count: int
    test_bars_count: int
    result: Optional[BacktestResult] = None
    adapter_stats: Optional[dict] = None
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.result is not None

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "window_id": self.window_id,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "train_bars": self.train_bars_count,
            "test_bars": self.test_bars_count,
            "succeeded": self.succeeded,
        }
        if self.result is not None:
            out.update({
                "initial_equity": self.result.initial_equity,
                "final_equity": round(self.result.final_equity, 4),
                "total_return_pct": round(self.result.total_return_pct, 4),
                "trade_count": self.result.trade_count,
                "win_rate": round(self.result.win_rate, 4),
                "max_drawdown_pct": round(self.result.max_drawdown_pct, 4),
                "kernel_rejections": len(self.result.rejections),
            })
        if self.adapter_stats is not None:
            out["adapter_stats"] = self.adapter_stats
        if self.error is not None:
            out["error"] = self.error
        return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class WalkForwardReport:
    """Aggregated multi-window output."""

    def __init__(
        self,
        config: WalkForwardConfig,
        windows: List[WalkForwardWindow],
        symbol: str,
        total_bars: int,
    ):
        self.config = config
        self.windows = windows
        self.symbol = symbol
        self.total_bars = total_bars
        self._aggregate: Optional[Dict[str, Any]] = None

    @property
    def successful_windows(self) -> List[WalkForwardWindow]:
        return [w for w in self.windows if w.succeeded]

    @property
    def failed_windows(self) -> List[WalkForwardWindow]:
        return [w for w in self.windows if not w.succeeded]

    @property
    def aggregate(self) -> Dict[str, Any]:
        if self._aggregate is None:
            self._aggregate = self._compute_aggregate()
        return self._aggregate

    def _compute_aggregate(self) -> Dict[str, Any]:
        succ = self.successful_windows
        if not succ:
            return {
                "windows_total": len(self.windows),
                "windows_succeeded": 0,
                "windows_failed": len(self.failed_windows),
                "trades_total": 0,
                "windows_profitable": 0,
                "windows_unprofitable": 0,
            }
        returns = [w.result.total_return_pct for w in succ if w.result]   # %
        trades = [w.result.trade_count for w in succ if w.result]
        max_dds = [w.result.max_drawdown_pct for w in succ if w.result]
        win_rates = [w.result.win_rate for w in succ if w.result and w.result.trade_count > 0]

        profitable = sum(1 for r in returns if r > 0)
        unprofitable = sum(1 for r in returns if r < 0)

        out: Dict[str, Any] = {
            "windows_total": len(self.windows),
            "windows_succeeded": len(succ),
            "windows_failed": len(self.failed_windows),
            "trades_total": sum(trades),
            "trades_mean_per_window": round(statistics.mean(trades), 2) if trades else 0,
            "windows_profitable": profitable,
            "windows_unprofitable": unprofitable,
            "windows_neutral": len(returns) - profitable - unprofitable,
            "return_pct_mean": round(statistics.mean(returns), 4),
            "return_pct_median": round(statistics.median(returns), 4),
            "return_pct_min": round(min(returns), 4),
            "return_pct_max": round(max(returns), 4),
            "max_drawdown_pct_worst": round(max(max_dds), 4) if max_dds else 0,
            "max_drawdown_pct_mean": round(statistics.mean(max_dds), 4) if max_dds else 0,
        }
        if len(returns) > 1:
            out["return_pct_stdev"] = round(statistics.stdev(returns), 4)
        if win_rates:
            out["win_rate_mean"] = round(statistics.mean(win_rates), 4)
        return out

    def summary(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_bars": self.total_bars,
            "config": asdict(self.config),
            "windows": [w.summary() for w in self.windows],
            "aggregate": self.aggregate,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.summary(), indent=indent, default=str)

    def to_json_file(self, path) -> None:
        Path(path).write_text(self.to_json())


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class WalkForwardHarness:
    """Rolling walk-forward orchestrator.

    Per window:
      1. Slice bars into train + test (anti-lookahead naturally enforced
         by providers using bars strictly before query timestamp).
      2. Build providers from full (train+test) bar history — gives
         indicators proper warmup but they can't see future of any query.
      3. Build fresh adapter via factory.
      4. Run ReplayEngineV2 on test slice only.
      5. Collect per-window result; on error, skip if fail_fast=False.
    """

    def __init__(
        self,
        bars: Sequence[Bar],
        config: WalkForwardConfig,
        adapter_factory: Callable[[], ProductionAdapter],
        indicators_provider_factory: Optional[Callable[[List[Bar]], Optional[Callable]]] = None,
        regime_provider_factory: Optional[Callable[[List[Bar]], Optional[Callable]]] = None,
        symbol: str = "ETHUSDT",
        initial_equity: float = 1000.0,
    ):
        if not bars:
            raise ValueError("WalkForwardHarness requires at least 1 bar")
        # Defensive sort + immutability
        self._bars: List[Bar] = sorted(bars, key=lambda b: b.timestamp)
        self._bar_timestamps: List[datetime] = [b.timestamp for b in self._bars]
        self.config = config
        self.adapter_factory = adapter_factory
        self.indicators_provider_factory = indicators_provider_factory
        self.regime_provider_factory = regime_provider_factory
        self.symbol = symbol
        self.initial_equity = initial_equity

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self) -> WalkForwardReport:
        windows = self._build_window_specs()
        if not windows:
            logger.warning(
                "No windows could be built — data span (%s → %s) too short for "
                "train=%d days + test=%d days.",
                self._bar_timestamps[0], self._bar_timestamps[-1],
                self.config.train_window_days, self.config.test_window_days,
            )
            return WalkForwardReport(
                config=self.config, windows=[], symbol=self.symbol,
                total_bars=len(self._bars),
            )

        results: List[WalkForwardWindow] = []
        for window_id, (train_start, train_end, test_start, test_end) in enumerate(windows):
            window = self._run_window(
                window_id, train_start, train_end, test_start, test_end,
            )
            results.append(window)

            if not window.succeeded and self.config.fail_fast:
                logger.error(
                    "Window %d failed (fail_fast=True): %s",
                    window_id, window.error,
                )
                raise RuntimeError(f"Walk-forward window {window_id} failed: {window.error}")

        return WalkForwardReport(
            config=self.config, windows=results, symbol=self.symbol,
            total_bars=len(self._bars),
        )

    # -----------------------------------------------------------------------
    # Window planning
    # -----------------------------------------------------------------------

    def _build_window_specs(self) -> List[tuple]:
        """Return list of (train_start, train_end, test_start, test_end)."""
        if len(self._bars) < 2:
            return []

        data_start = self._bar_timestamps[0]
        data_end = self._bar_timestamps[-1]

        train_delta = timedelta(days=self.config.train_window_days)
        test_delta = timedelta(days=self.config.test_window_days)
        step_delta = timedelta(days=self.config.step_days)

        windows: List[tuple] = []
        train_start = data_start
        while True:
            train_end = train_start + train_delta
            test_start = train_end
            test_end = test_start + test_delta

            if test_end > data_end:
                break

            windows.append((train_start, train_end, test_start, test_end))
            train_start = train_start + step_delta

        return windows

    # -----------------------------------------------------------------------
    # Single window execution
    # -----------------------------------------------------------------------

    def _run_window(
        self,
        window_id: int,
        train_start: datetime,
        train_end: datetime,
        test_start: datetime,
        test_end: datetime,
    ) -> WalkForwardWindow:
        # 1. Slice bars
        train_bars = self._slice_bars(train_start, train_end)
        test_bars = self._slice_bars(test_start, test_end)

        window = WalkForwardWindow(
            window_id=window_id,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            train_bars_count=len(train_bars),
            test_bars_count=len(test_bars),
        )

        # 2. Sanity: enough bars in test slice
        if len(test_bars) < self.config.min_bars_in_test:
            window.error = (
                f"insufficient test bars: {len(test_bars)} < "
                f"min_bars_in_test={self.config.min_bars_in_test}"
            )
            logger.warning("Window %d skipped: %s", window_id, window.error)
            return window

        try:
            # 3. Build providers with FULL (train+test) bar history for warmup.
            #    Anti-lookahead is enforced by providers internally.
            warmup_plus_test = train_bars + test_bars

            indicators_callable = None
            if self.indicators_provider_factory is not None:
                indicators_callable = self.indicators_provider_factory(warmup_plus_test)

            regime_callable = None
            if self.regime_provider_factory is not None:
                regime_callable = self.regime_provider_factory(warmup_plus_test)

            # 4. Build fresh adapter via factory (clean state per window)
            adapter = self.adapter_factory()

            # 5. Engine instance per window — kernel inside adapter is fresh
            engine = ReplayEngineV2(
                symbol=self.symbol,
                initial_equity=self.initial_equity,
                regime_provider=regime_callable,
                indicators_provider=indicators_callable,
                **self.config.engine_kwargs,
            )

            # 6. Run on test slice only
            result = engine.run(test_bars, adapter)

            window.result = result
            window.adapter_stats = adapter.get_stats() if hasattr(adapter, "get_stats") else None

        except Exception as e:
            window.error = f"{type(e).__name__}: {e}"
            logger.exception("Window %d failed: %s", window_id, e)

        return window

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _slice_bars(self, start: datetime, end: datetime) -> List[Bar]:
        """Bars where start <= timestamp < end."""
        i_start = bisect.bisect_left(self._bar_timestamps, start)
        i_end = bisect.bisect_left(self._bar_timestamps, end)
        return self._bars[i_start:i_end]
