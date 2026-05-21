"""ProductionAdapter integration tests (Phase 6.3.1a Step 4).

Tests the bridge from production-like BaseStrategy → ReplayEngineV2.
Uses a MockStrategy that emulates the BaseStrategy contract minimally
so tests don't depend on the full production stack.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, ReplayEngineV2, SnapshotContext, Position, Fill,
    OpenAction, ScaleInAction, ReduceAction, CloseAction,
    ProductionAdapter, BacktestRiskKernel, BacktestClock, TradeDecision,
    PROD_ENTER_LONG, PROD_ENTER_SHORT, PROD_EXIT, PROD_HOLD,
    PROD_SCALE_IN, PROD_REDUCE,
)


T0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Mock production strategy + signal (emulates BaseStrategy minimally)
# ---------------------------------------------------------------------------

class MockSignalType(Enum):
    ENTER_LONG  = "enter_long"
    ENTER_SHORT = "enter_short"
    SCALE_IN    = "scale_in"
    REDUCE      = "reduce"
    EXIT        = "exit"
    HOLD        = "hold"


@dataclass
class MockSignal:
    signal_type: MockSignalType
    size_usd: float = 100.0
    confidence: float = 0.7
    strategy_id: str = "mock_strategy"
    reason: str = "test"
    metadata: dict = field(default_factory=dict)

    def is_actionable(self) -> bool:
        return self.signal_type != MockSignalType.HOLD


class MockStrategy:
    """Minimum production-like strategy for adapter testing."""

    def __init__(self, capital_pct: float = 1.0, enabled: bool = True, **_):
        self.capital_pct = capital_pct
        self.enabled = enabled
        self._next_signal: Optional[MockSignal] = None
        self._capital: float = 0.0
        self.fills_received: List[tuple] = []
        self.closes_received: List[tuple] = []
        self.gate_calls: List[datetime] = []

    def set_next_signal(self, signal: Optional[MockSignal]):
        self._next_signal = signal

    def set_strategy_capital(self, capital: float):
        self._capital = capital

    def get_strategy_id(self) -> str:
        return "mock_strategy_v1"

    def evaluate(self, symbol, market_data, regime) -> Optional[MockSignal]:
        return self._next_signal

    def apply_risk_gates(self, signal, regime, now=None) -> Optional[MockSignal]:
        if now is not None:
            self.gate_calls.append(now)
        return signal

    def on_tier_filled(self, symbol, tier, price, size_usd, now=None):
        self.fills_received.append((symbol, tier, price, size_usd, now))

    def on_position_closed(self, symbol, pnl_usd, was_loss, now=None):
        self.closes_received.append((symbol, pnl_usd, was_loss, now))


class MockAdapter(ProductionAdapter):
    """Concrete subclass for testing — implements the 3 abstract methods."""
    name = "test_adapter"
    strategy_class = MockStrategy

    def prepare_market_data(self, snapshot):
        return {"last_price": snapshot.spot, "vix_level": 20.0}

    def get_stop_loss_pct(self, prod_signal, market_data):
        return 0.02

    def get_take_profit_pct(self, prod_signal, market_data):
        return 0.04


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _snapshot(spot: float = 2000.0, equity: float = 1000.0,
              positions_count: int = 0, regime: Optional[str] = "NORMAL",
              ts_offset_min: int = 0) -> SnapshotContext:
    ts = T0 + timedelta(minutes=ts_offset_min)
    bar = Bar(timestamp=ts, open=spot, high=spot * 1.001, low=spot * 0.999,
              close=spot, volume=1000.0)
    return SnapshotContext(
        timestamp=ts, symbol="ETHUSDT", bar=bar, spot=spot,
        equity=equity, open_position_count=positions_count, regime=regime,
    )


@pytest.fixture
def adapter():
    return MockAdapter(starting_capital_usd=1000.0)


# ===========================================================================
# Construction
# ===========================================================================

class TestConstruction:

    def test_requires_strategy_class(self):
        class IncompleteAdapter(ProductionAdapter):
            strategy_class = None
            def prepare_market_data(self, s): return None
            def get_stop_loss_pct(self, p, m): return 0.02
            def get_take_profit_pct(self, p, m): return 0.04
        with pytest.raises(ValueError, match="strategy_class"):
            IncompleteAdapter()

    def test_default_kernel_created(self, adapter):
        assert adapter.risk_kernel is not None
        assert adapter.risk_kernel.starting_equity == 1000.0

    def test_clock_shared_between_adapter_and_kernel(self, adapter):
        assert adapter.clock is adapter.risk_kernel.clock

    def test_explicit_kernel_used(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=5000.0, clock=clock)
        adapter = MockAdapter(starting_capital_usd=5000.0, risk_kernel=kernel, clock=clock)
        assert adapter.risk_kernel is kernel


# ===========================================================================
# Signal → Action translation
# ===========================================================================

class TestSignalTranslation:

    def test_enter_long_returns_open_action(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.ENTER_LONG))
        actions = adapter.evaluate(_snapshot(), positions=[])
        assert len(actions) == 1
        assert isinstance(actions[0], OpenAction)
        assert actions[0].side == "LONG"
        # qty = approved_size / spot. approved=100 (under all caps), spot=2000
        assert actions[0].qty == pytest.approx(100.0 / 2000.0)

    def test_enter_short_returns_open_action(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.ENTER_SHORT))
        actions = adapter.evaluate(_snapshot(), positions=[])
        assert len(actions) == 1
        assert isinstance(actions[0], OpenAction)
        assert actions[0].side == "SHORT"

    def test_scale_in_with_existing_position(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.SCALE_IN))
        existing = Position(id="abc123", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, size=0.05, avg_entry_price=2000.0)
        actions = adapter.evaluate(_snapshot(), positions=[existing])
        assert len(actions) == 1
        assert isinstance(actions[0], ScaleInAction)
        assert actions[0].position_id == "abc123"

    def test_scale_in_without_position_falls_back_to_open(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.SCALE_IN))
        actions = adapter.evaluate(_snapshot(), positions=[])
        # Defensive fallback
        assert len(actions) == 1
        assert isinstance(actions[0], OpenAction)

    def test_exit_returns_close_action(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.EXIT))
        existing = Position(id="xyz789", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, size=0.05, avg_entry_price=2000.0)
        actions = adapter.evaluate(_snapshot(), positions=[existing])
        assert len(actions) == 1
        assert isinstance(actions[0], CloseAction)
        assert actions[0].position_id == "xyz789"

    def test_reduce_returns_reduce_action(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.REDUCE, size_usd=50.0))
        existing = Position(id="r1", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, size=0.1, avg_entry_price=2000.0)
        actions = adapter.evaluate(_snapshot(), positions=[existing])
        assert len(actions) == 1
        assert isinstance(actions[0], ReduceAction)
        assert actions[0].position_id == "r1"

    def test_hold_returns_empty_list(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.HOLD))
        actions = adapter.evaluate(_snapshot(), positions=[])
        assert actions == []


# ===========================================================================
# Kernel integration
# ===========================================================================

class TestKernelIntegration:

    def test_halted_kernel_blocks_signal(self, adapter):
        # Sync adapter clock to T0 so halt anchor lines up with snapshot timestamp
        adapter.clock.set(T0)
        # Trigger daily DD halt
        adapter.risk_kernel.record_trade_outcome(-50.0)
        assert not adapter.risk_kernel.is_trading_allowed()

        adapter.strategy.set_next_signal(MockSignal(MockSignalType.ENTER_LONG))
        actions = adapter.evaluate(_snapshot(), positions=[])
        assert actions == []
        assert adapter._signals_kernel_halted == 1

    def test_rejected_kernel_blocks_signal(self, adapter):
        # Stop-loss too wide → kernel REJECTED
        class WideStopAdapter(MockAdapter):
            def get_stop_loss_pct(self, p, m):
                return 0.30   # > 25% sanity cap
        ad = WideStopAdapter(starting_capital_usd=1000.0)
        ad.strategy.set_next_signal(MockSignal(MockSignalType.ENTER_LONG))
        actions = ad.evaluate(_snapshot(), positions=[])
        assert actions == []
        assert ad._signals_kernel_rejected == 1

    def test_reduced_size_propagates_to_action_qty(self, adapter):
        """Proposed=500, max_pos=250 → reduced to 250 → qty=125/2000."""
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.ENTER_LONG, size_usd=500.0))
        actions = adapter.evaluate(_snapshot(), positions=[])
        assert len(actions) == 1
        # approved = min(500, risk_based=500, max=250) = 250
        assert actions[0].qty == pytest.approx(250.0 / 2000.0)
        assert adapter._signals_kernel_reduced == 1


# ===========================================================================
# Callbacks (on_fill, on_position_closed)
# ===========================================================================

class TestCallbacks:

    def test_on_fill_propagates_to_strategy(self, adapter):
        position = Position(id="p1", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, avg_entry_price=2000.0)
        fill = Fill(timestamp=T0, price=2000.0, qty=0.05, commission=0.15)
        position.add_fill(fill)
        adapter.on_fill(position, fill)
        assert len(adapter.strategy.fills_received) == 1
        symbol, tier, price, size_usd, now = adapter.strategy.fills_received[0]
        assert symbol == "ETHUSDT"
        assert tier == 1
        assert price == 2000.0

    def test_on_position_closed_drives_kernel(self, adapter):
        position = Position(id="p1", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, avg_entry_price=2000.0,
                            realized_pnl=-25.0, closed_at=T0 + timedelta(hours=1))
        adapter.on_position_closed(position)
        # Strategy received the close
        assert len(adapter.strategy.closes_received) == 1
        # Kernel recorded the loss
        status = adapter.risk_kernel.get_status()
        assert status["total_pnl"] == -25.0
        assert status["consecutive_losses"] == 1

    def test_on_position_closed_win_resets_loss_streak(self, adapter):
        # First, a loss
        pos1 = Position(id="p1", symbol="ETHUSDT", side="LONG",
                        opened_at=T0, avg_entry_price=2000.0,
                        realized_pnl=-10.0, closed_at=T0 + timedelta(hours=1))
        adapter.on_position_closed(pos1)
        assert adapter.risk_kernel.get_status()["consecutive_losses"] == 1

        # Then a win
        pos2 = Position(id="p2", symbol="ETHUSDT", side="LONG",
                        opened_at=T0 + timedelta(hours=2),
                        avg_entry_price=2000.0, realized_pnl=15.0,
                        closed_at=T0 + timedelta(hours=3))
        adapter.on_position_closed(pos2)
        assert adapter.risk_kernel.get_status()["consecutive_losses"] == 0


# ===========================================================================
# Reset (walk-forward window boundary)
# ===========================================================================

class TestReset:

    def test_reset_clears_kernel_and_stats(self, adapter):
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.ENTER_LONG))
        adapter.evaluate(_snapshot(), positions=[])
        adapter.risk_kernel.record_trade_outcome(-10.0)
        assert adapter._signals_emitted > 0

        adapter.reset()

        assert adapter._signals_emitted == 0
        assert adapter.risk_kernel.get_status()["total_pnl"] == 0.0
        assert adapter.risk_kernel.get_status()["consecutive_losses"] == 0


# ===========================================================================
# End-to-end: full ReplayEngine v2 + adapter + kernel
# ===========================================================================

class TestEndToEnd:

    def test_full_engine_run_with_hold_signals(self, adapter):
        """20 bars, strategy always HOLD → 0 trades, 0 kernel approvals."""
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.HOLD))
        bars = [
            Bar(timestamp=T0 + timedelta(minutes=5 * i),
                open=2000, high=2001, low=1999, close=2000, volume=1000.0)
            for i in range(20)
        ]
        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0)
        result = eng.run(bars, adapter)
        assert result.trade_count == 0
        assert adapter.risk_kernel.get_status()["approvals_count"] == 0

    def test_full_engine_run_with_entry_and_exit(self, adapter):
        """Entry at bar 0, hold for bars 1-3, exit at bar 4 → 1 trade."""
        bars = [
            Bar(timestamp=T0 + timedelta(minutes=5 * i),
                open=2000 + i, high=2002 + i, low=1998 + i,
                close=2000 + i, volume=1000.0)
            for i in range(8)
        ]

        # Sequence: ENTER, HOLD x 5, EXIT, HOLD
        call_count = [0]
        def patched_evaluate(symbol, market_data, regime):
            call_count[0] += 1
            if call_count[0] == 1:
                return MockSignal(MockSignalType.ENTER_LONG, size_usd=50.0)
            if call_count[0] == 7:
                return MockSignal(MockSignalType.EXIT)
            return MockSignal(MockSignalType.HOLD)

        adapter.strategy.evaluate = patched_evaluate

        eng = ReplayEngineV2(symbol="ETHUSDT", initial_equity=1000.0,
                             slippage_bps=0, commission_bps=0)
        result = eng.run(bars, adapter)

        # 1 trade closed (entry → exit)
        assert result.trade_count == 1
        # Kernel was called ONCE for the entry. EXIT/REDUCE don't go through kernel
        # (de-risking doesn't allocate capital → no approval needed). Phase 6.3.1a
        # Step 4 semantic improvement over coleague's adapter.
        assert adapter.risk_kernel.get_status()["approvals_count"] == 1
        # Kernel recorded outcome on close
        assert adapter.risk_kernel.get_status()["fills_count"] == 1

    def test_exit_signal_does_not_call_kernel(self, adapter):
        """EXIT is de-risking — kernel.approve_trade should not be called."""
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.EXIT))
        existing = Position(id="p1", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, size=0.05, avg_entry_price=2000.0)
        actions = adapter.evaluate(_snapshot(), positions=[existing])
        assert len(actions) == 1
        assert isinstance(actions[0], CloseAction)
        # Crucially: kernel was NOT approached
        assert adapter.risk_kernel.get_status()["approvals_count"] == 0

    def test_reduce_signal_does_not_call_kernel(self, adapter):
        """REDUCE is de-risking — kernel.approve_trade should not be called."""
        adapter.strategy.set_next_signal(MockSignal(MockSignalType.REDUCE, size_usd=25.0))
        existing = Position(id="p1", symbol="ETHUSDT", side="LONG",
                            opened_at=T0, size=0.1, avg_entry_price=2000.0)
        actions = adapter.evaluate(_snapshot(), positions=[existing])
        assert len(actions) == 1
        assert isinstance(actions[0], ReduceAction)
        assert adapter.risk_kernel.get_status()["approvals_count"] == 0
