"""Tests for Phase 6.3.1b-A audit fixes (Q6.1 + Q6.3)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, IndicatorsProvider, IndicatorsConfig,
    ReplayEngineV2, SnapshotContext,
)
from bot.backtest.adapters import MeanReversionAdapter


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _bars(n: int, start_price: float = 2000.0, slope: float = 0.0) -> List[Bar]:
    bars = []
    price = start_price
    for i in range(n):
        bars.append(Bar(
            timestamp=T0 + timedelta(minutes=5 * i),
            open=price, high=price + 1, low=price - 1, close=price + slope,
            volume=1000,
        ))
        price += slope
    return bars


# ===========================================================================
# Q6.1 — close_1h_history exposure
# ===========================================================================

class TestQ61CloseHistoryExposure:

    def test_close_1h_history_present_after_warmup(self):
        bars = _bars(300, slope=0.5)
        ind = IndicatorsProvider(bars)
        result = ind.indicators_at(bars[-1].timestamp + timedelta(minutes=1))
        assert "close_1h_history" in result
        assert isinstance(result["close_1h_history"], list)
        assert len(result["close_1h_history"]) > 0

    def test_close_1h_history_respects_config_size(self):
        bars = _bars(500, slope=0.5)
        ind = IndicatorsProvider(bars, IndicatorsConfig(close_1h_history_size=10))
        result = ind.indicators_at(bars[-1].timestamp + timedelta(minutes=1))
        # Should be capped at 10
        assert len(result["close_1h_history"]) <= 10

    def test_close_1h_history_excludes_current_bar(self):
        """Anti-lookahead: the list should not include the current-or-future bars."""
        bars = _bars(300, slope=0.5)
        ind = IndicatorsProvider(bars)
        # Query at bar 100's timestamp — bar 100 not visible (strictly-before)
        result = ind.indicators_at(bars[100].timestamp)
        # All closes in history must be < bars[100].close
        for c in result["close_1h_history"]:
            assert c <= bars[100].close

    def test_close_1h_history_empty_during_warmup(self):
        """Insufficient data → close_1h_history absent or empty."""
        bars = _bars(10)
        ind = IndicatorsProvider(bars)
        # Query AT first bar's timestamp — strictly-before semantics means nothing visible
        result = ind.indicators_at(bars[0].timestamp)
        # close_1h_history may be absent OR empty list — both acceptable during warmup
        ch = result.get("close_1h_history", [])
        assert ch == []

    def test_close_1h_history_size_validation(self):
        with pytest.raises(ValueError, match="close_1h_history_size"):
            IndicatorsConfig(close_1h_history_size=0)


# ===========================================================================
# Q6.3 — per-tick capital sync
# ===========================================================================

class _ProbeStrategy:
    """Tracks set_strategy_capital calls for Q6.3 verification."""
    def __init__(self, capital_pct: float = 1.0, **kwargs):
        self.capital_pct = capital_pct
        self.set_capital_calls: list = []
        self.evaluate_calls: list = []

    def set_strategy_capital(self, capital: float) -> None:
        self.set_capital_calls.append(capital)

    def get_strategy_id(self) -> str:
        return "probe"

    def evaluate(self, symbol, market_data, regime):
        self.evaluate_calls.append((market_data.get("rsi_14_1h"),
                                     self.set_capital_calls[-1] if self.set_capital_calls else None))
        from bot.backtest.adapters.mean_reversion_adapter import MockSignal, MockSignalType
        return MockSignal(MockSignalType.HOLD, 0.0, 0.0, reason="probe")

    def apply_risk_gates(self, signal, regime, now=None):
        return signal


class _ProbeAdapter(MeanReversionAdapter):
    strategy_class = _ProbeStrategy


class TestQ63PerTickCapitalSync:

    def test_set_strategy_capital_called_per_tick(self):
        """Each evaluate() tick should call set_strategy_capital with current equity * capital_pct.
        Requires trending bars so RSI computable → MeanReversionAdapter.prepare_market_data
        returns dict → strategy.evaluate runs → sync fires before each evaluate."""
        bars = _bars(300, slope=0.5)   # trending so RSI computable
        adapter = _ProbeAdapter(starting_capital_usd=200.0)
        indicators = IndicatorsProvider(bars)
        engine = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            indicators_provider=indicators.callable_for_engine(),
        )
        engine.run(bars, adapter)

        strategy = adapter.strategy
        # __init__ call + many per-tick calls expected
        assert len(strategy.set_capital_calls) > 10, (
            f"set_strategy_capital only called {len(strategy.set_capital_calls)} times — "
            f"per-tick sync not working"
        )

    def test_capital_sync_uses_kernel_current_equity(self):
        """Verify the synced value = current_equity * capital_pct."""
        bars = _bars(300, slope=0.5)
        adapter = _ProbeAdapter(starting_capital_usd=200.0)
        adapter.strategy.capital_pct = 0.5    # 50% allocation
        indicators = IndicatorsProvider(bars)
        engine = ReplayEngineV2(
            symbol="ETHUSDT", initial_equity=200.0,
            indicators_provider=indicators.callable_for_engine(),
        )
        engine.run(bars, adapter)

        calls = adapter.strategy.set_capital_calls
        # Init call uses capital_pct=1.0 (default at init time): 200
        # Per-tick calls use capital_pct=0.5: 200 * 0.5 = 100 (no PnL on probe → equity flat)
        post_init_calls = [c for c in calls if abs(c - 100.0) < 0.01]
        assert len(post_init_calls) > 0, (
            f"Expected ≥1 sync at 100.0 (200 × 0.5), got first 10 calls: {calls[:10]}"
        )
