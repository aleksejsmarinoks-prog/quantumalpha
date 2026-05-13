"""Tests for bot.backtester.execution_sim."""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import pytest

from bot.backtester.execution_sim import (
    DEFAULT_KYLE_K,
    MAKER_FEE_BP,
    TAKER_FEE_BP,
    ExecutionSimulator,
    MarketSnapshot,
    apply_slippage_to_price,
    compute_fee_usd,
    compute_slippage_bp,
    kline_spread_bp,
)
from bot.backtester.models import OrderType, Side


@pytest.fixture
def snap() -> MarketSnapshot:
    return MarketSnapshot(
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        symbol="ETHUSDT",
        mid_price=3500.0,
        high=3505.0,
        low=3495.0,
        adv_24h_usd=10_000_000.0,
    )


class TestPureHelpers:
    def test_kline_spread_bp(self, snap):
        # range = 10, mid = 3500 → spread_bp = 10/3500 * 10000 ≈ 28.57
        assert kline_spread_bp(snap.high, snap.low, snap.mid_price) == pytest.approx(28.57, abs=0.1)

    def test_kline_spread_bp_zero_mid(self):
        assert kline_spread_bp(100, 90, 0) == 0.0

    def test_compute_fee_usd_maker(self):
        assert compute_fee_usd(1000.0, OrderType.MAKER) == pytest.approx(1000 * MAKER_FEE_BP / 10000)

    def test_compute_fee_usd_taker(self):
        assert compute_fee_usd(1000.0, OrderType.TAKER) == pytest.approx(1000 * TAKER_FEE_BP / 10000)

    def test_apply_slippage_buy_slips_up(self):
        p = apply_slippage_to_price(3500.0, 10.0, Side.BUY)
        assert p > 3500.0

    def test_apply_slippage_sell_slips_down(self):
        p = apply_slippage_to_price(3500.0, 10.0, Side.SELL)
        assert p < 3500.0


class TestSlippageFormula:
    def test_slippage_zero_size(self, snap):
        s = compute_slippage_bp(snap, 0.0)
        assert s >= 0
        # Should equal half spread only
        assert s == pytest.approx(kline_spread_bp(snap.high, snap.low, snap.mid_price) / 2.0)

    def test_slippage_scales_with_size(self, snap):
        small = compute_slippage_bp(snap, 100.0)
        large = compute_slippage_bp(snap, 100_000.0)
        assert large > small

    def test_slippage_with_zero_adv(self, snap):
        zero_adv = MarketSnapshot(snap.timestamp, snap.symbol, snap.mid_price, snap.high, snap.low, 0.0)
        s = compute_slippage_bp(zero_adv, 1000.0)
        # No ADV → only spread floor
        assert s >= 0.0


class TestExecutionSimulator:
    def test_invalid_maker_fill_rate_rejected(self):
        with pytest.raises(ValueError):
            ExecutionSimulator(maker_fill_rate=1.5)

    def test_zero_size_returns_none(self, snap):
        sim = ExecutionSimulator(seed=1)
        fill = sim.execute_order(snap, Side.BUY, 0.0, OrderType.TAKER)
        assert fill is None

    def test_taker_always_fills(self, snap):
        sim = ExecutionSimulator(seed=1)
        fill = sim.execute_order(snap, Side.BUY, 200.0, OrderType.TAKER)
        assert fill is not None
        assert fill.order_type == OrderType.TAKER
        assert fill.fill_price > snap.mid_price                # BUY slips up

    def test_maker_can_fail(self, snap):
        # With fill_rate=0.0, every maker order must miss
        sim = ExecutionSimulator(maker_fill_rate=0.0, seed=1)
        fill = sim.execute_order(snap, Side.BUY, 200.0, OrderType.MAKER)
        assert fill is None

    def test_maker_always_fills_when_rate_1(self, snap):
        sim = ExecutionSimulator(maker_fill_rate=1.0, seed=1)
        fill = sim.execute_order(snap, Side.BUY, 200.0, OrderType.MAKER)
        assert fill is not None
        assert fill.order_type == OrderType.MAKER

    def test_maker_fee_less_than_taker(self, snap):
        sim = ExecutionSimulator(maker_fill_rate=1.0, seed=1)
        maker = sim.execute_order(snap, Side.BUY, 1000.0, OrderType.MAKER)
        taker = sim.execute_order(snap, Side.BUY, 1000.0, OrderType.TAKER)
        assert maker is not None and taker is not None
        assert maker.fee_usd < taker.fee_usd

    def test_deterministic_with_seed(self, snap):
        sim1 = ExecutionSimulator(maker_fill_rate=0.5, seed=42)
        sim2 = ExecutionSimulator(maker_fill_rate=0.5, seed=42)
        results1 = [sim1.execute_order(snap, Side.BUY, 200.0, OrderType.MAKER) is not None for _ in range(10)]
        results2 = [sim2.execute_order(snap, Side.BUY, 200.0, OrderType.MAKER) is not None for _ in range(10)]
        assert results1 == results2

    def test_buy_sell_symmetry(self, snap):
        sim = ExecutionSimulator(seed=1)
        b = sim.execute_order(snap, Side.BUY, 500.0, OrderType.TAKER)
        s = sim.execute_order(snap, Side.SELL, 500.0, OrderType.TAKER)
        assert b is not None and s is not None
        assert b.fill_price > snap.mid_price
        assert s.fill_price < snap.mid_price

    def test_reset_rng_restores_determinism(self, snap):
        sim = ExecutionSimulator(maker_fill_rate=0.5, seed=7)
        first = [sim.execute_order(snap, Side.BUY, 200.0, OrderType.MAKER) is not None for _ in range(5)]
        sim.reset_rng(7)
        second = [sim.execute_order(snap, Side.BUY, 200.0, OrderType.MAKER) is not None for _ in range(5)]
        assert first == second
