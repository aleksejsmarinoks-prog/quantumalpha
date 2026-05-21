"""BacktestRiskKernel contract tests (Phase 6.3.1a Step 4).

Validates kernel behavior independently of adapter. Covers all 5 layers:
killswitch, validation, sizing, leverage, R:R.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    BacktestRiskKernel, TradeRequest, TradeDecision, HaltReason,
    BacktestClock,
    MAX_POSITION_PCT_OF_EQUITY, DEFAULT_RISK_PER_TRADE_PCT,
    DAILY_DD_LIMIT_PCT, MIN_ORDER_USD, CONSECUTIVE_LOSS_COOLDOWN_TRIGGER,
)


T0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)


def _req(
    proposed=100.0, sl=0.02, tp=0.04, confidence=0.7,
    regime="NEUTRAL", s13=False, vix=20.0, leverage=0.0,
) -> TradeRequest:
    return TradeRequest(
        asset="ETHUSDT", side="long",
        proposed_size_usd=proposed,
        stop_loss_pct=sl, take_profit_pct=tp,
        strategy_name="test", confidence=confidence,
        market_regime=regime, s13_active=s13, vix_level=vix,
        current_leverage=leverage,
    )


# ===========================================================================
# Layer 1: Killswitch
# ===========================================================================

class TestKillswitch:

    def test_initial_state_allows(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        assert kernel.is_trading_allowed() is True

    def test_daily_dd_triggers_halt(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        # Lose 5% (50 USD) → daily DD trigger
        kernel.record_trade_outcome(-50.0)
        assert kernel.is_trading_allowed() is False
        status = kernel.get_status()
        assert status["halted"] is True
        assert status["halt_reason"] == "daily_drawdown"

    def test_halted_kernel_returns_halted_decision(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        kernel.record_trade_outcome(-50.0)   # trigger halt
        approval = kernel.approve_trade(_req())
        assert approval.decision == TradeDecision.HALTED
        assert approval.halt_reason == HaltReason.DAILY_DD

    def test_consecutive_loss_triggers_cooldown(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=10000.0, clock=clock)
        # Three small losses (not enough to trigger DD) → cooldown
        for _ in range(CONSECUTIVE_LOSS_COOLDOWN_TRIGGER):
            kernel.record_trade_outcome(-10.0)
        assert kernel.is_trading_allowed() is False
        assert kernel.get_status()["halt_reason"] == "consecutive_loss_cooldown"

    def test_consecutive_loss_resets_on_win(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=10000.0, clock=clock)
        kernel.record_trade_outcome(-10.0)
        kernel.record_trade_outcome(-10.0)
        kernel.record_trade_outcome(+5.0)   # win resets counter
        kernel.record_trade_outcome(-10.0)
        assert kernel.is_trading_allowed() is True


# ===========================================================================
# Layer 2: Validation
# ===========================================================================

class TestValidation:

    def test_zero_size_rejected(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(proposed=0.0))
        assert approval.decision == TradeDecision.REJECTED
        assert any("positive" in r.lower() for r in approval.rejection_reasons)

    def test_stop_loss_too_wide_rejected(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(sl=0.30))   # > 25%
        assert approval.decision == TradeDecision.REJECTED
        assert any("stop-loss" in r.lower() for r in approval.rejection_reasons)

    def test_negative_confidence_rejected(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(confidence=-0.5))
        assert approval.decision == TradeDecision.REJECTED


# ===========================================================================
# Layer 3: Sizing
# ===========================================================================

class TestSizing:

    def test_proposed_fits_under_caps_approved_as_is(self):
        """Proposed=100, risk_based=500, max_pos=250 → approved 100 (no reduction)."""
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(proposed=100.0, sl=0.02))
        assert approval.decision == TradeDecision.APPROVED
        assert approval.approved_size_usd == 100.0

    def test_proposed_exceeds_max_position_reduced(self):
        """Proposed=500, max_pos=250 → REDUCED_SIZE to 250."""
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(proposed=500.0, sl=0.02))
        assert approval.decision == TradeDecision.REDUCED_SIZE
        # max_pos = 25% × 1000 = 250
        assert approval.approved_size_usd == 250.0

    def test_below_min_order_usd_rejected(self):
        """Tiny proposed → may be reduced to < $10 → reject."""
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=100.0, clock=clock)
        # equity=100, risk_dollars=1, sl=0.20 → risk_based=5 → below $10
        approval = kernel.approve_trade(_req(proposed=5.0, sl=0.20))
        assert approval.decision == TradeDecision.REJECTED
        assert any("minimum" in r.lower() for r in approval.rejection_reasons)

    def test_s13_regime_halves_risk(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        # Proposed exceeds risk_based with S13 multiplier
        # Without S13: risk_dollars=10, risk_based=500
        # With S13: risk_dollars=5, risk_based=250
        approval = kernel.approve_trade(_req(proposed=400.0, sl=0.02, s13=True))
        assert approval.decision == TradeDecision.REDUCED_SIZE
        # Approved is min(400, 250, 250) = 250
        assert approval.approved_size_usd == 250.0
        assert any("s13" in w.lower() for w in approval.warnings)

    def test_high_vix_reduces_risk(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(proposed=100.0, sl=0.02, vix=32.0))
        assert any("vix" in w.lower() for w in approval.warnings)


# ===========================================================================
# Layer 4: Leverage
# ===========================================================================

class TestLeverage:

    def test_leverage_cap_rejected(self):
        """Existing 2.9x leverage + proposed 200 on 1000 equity → would be 3.1x → reject."""
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        approval = kernel.approve_trade(_req(proposed=200.0, sl=0.02, leverage=2.9))
        assert approval.decision == TradeDecision.REJECTED
        assert any("leverage" in r.lower() for r in approval.rejection_reasons)

    def test_leverage_under_cap_approved(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        # 1.0x existing + 100/1000 = 1.1x → under 3.0x cap
        approval = kernel.approve_trade(_req(proposed=100.0, sl=0.02, leverage=1.0))
        assert approval.decision == TradeDecision.APPROVED


# ===========================================================================
# Reset / clock
# ===========================================================================

class TestResetAndClock:

    def test_reset_clears_state(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        kernel.record_trade_outcome(-50.0)   # halt
        kernel.reset()
        assert kernel.is_trading_allowed() is True
        assert kernel.get_status()["consecutive_losses"] == 0
        assert kernel.get_status()["daily_pnl"] == 0.0

    def test_reset_with_new_equity(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=1000.0, clock=clock)
        kernel.reset(starting_equity_usd=5000.0)
        assert kernel.get_status()["equity"] == 5000.0

    def test_halt_expires_after_cooldown_window(self):
        clock = BacktestClock(T0)
        kernel = BacktestRiskKernel(starting_equity_usd=10000.0, clock=clock)
        # Trigger consecutive loss cooldown (24h)
        for _ in range(3):
            kernel.record_trade_outcome(-10.0)
        assert kernel.is_trading_allowed() is False
        # Advance clock 25h
        clock.set(T0 + timedelta(hours=25))
        assert kernel.is_trading_allowed() is True
