"""
Tests for lv1_self_critique — 12-factor SELF_CRITIQUE Gate.

Coverage:
  - Each of 12 factors triggered in isolation
  - Decision matrix transitions: PASS / HALF / BLOCK
  - Combination tests covering the threshold boundaries
  - Direction-symmetry checks (LONG vs SHORT)
"""
from __future__ import annotations

import pytest

from bot.strategies.lv1_models import Direction
from bot.strategies.lv1_self_critique import (
    CritiqueResult,
    evaluate,
    factor_1_funding_crowding,
    factor_2_s13_long_forbidden,
    factor_3_basis_anomaly,
    factor_4_btc_systemic_impulse,
    factor_5_btc_eth_corr_breakdown,
    factor_6_oi_spike_negative_price,
    factor_7_bvol_extreme,
    factor_8_microstructure_degraded,
    factor_9_funding_settlement_window,
    factor_10_cvd_broke,
    factor_11_dxy_long_crypto,
    factor_12_calendar_event,
)
from tests.conftest import build_market_state, build_qa


# ─────────────────────────────────────────────────────────────────────────────
# Individual factor tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFactor1FundingCrowding:
    def test_long_clear_no_flag(self):
        ms = build_market_state(funding=-0.0001)
        assert factor_1_funding_crowding(ms, Direction.LONG) is None

    def test_long_crowded_flagged(self):
        ms = build_market_state(funding=+0.0020)
        result = factor_1_funding_crowding(ms, Direction.LONG)
        assert result is not None
        name, penalty = result
        assert name == "F1_FUNDING_LONG_CROWDED"
        assert penalty == -0.20

    def test_short_crowded_flagged(self):
        ms = build_market_state(funding=-0.0015)
        result = factor_1_funding_crowding(ms, Direction.SHORT)
        assert result is not None
        assert result[0] == "F1_FUNDING_SHORT_CROWDED"

    def test_short_clear(self):
        ms = build_market_state(funding=+0.0001)
        assert factor_1_funding_crowding(ms, Direction.SHORT) is None


class TestFactor2S13LongForbidden:
    def test_s13_active_high_vix_long_blocks(self):
        qa = build_qa(s13=True, vix=35.0)
        result = factor_2_s13_long_forbidden(qa, Direction.LONG)
        assert result is not None
        assert result[0] == "F2_S13_VIX_LONG_FORBIDDEN"
        assert result[1] == -0.30

    def test_s13_active_low_vix_no_flag(self):
        qa = build_qa(s13=True, vix=25.0)
        assert factor_2_s13_long_forbidden(qa, Direction.LONG) is None

    def test_s13_active_short_no_flag(self):
        qa = build_qa(s13=True, vix=35.0)
        assert factor_2_s13_long_forbidden(qa, Direction.SHORT) is None

    def test_s13_inactive_no_flag(self):
        qa = build_qa(s13=False, vix=40.0)
        assert factor_2_s13_long_forbidden(qa, Direction.LONG) is None


class TestFactor3BasisAnomaly:
    def test_long_with_premium_hot_flagged(self):
        ms = build_market_state(spot_basis_bps=60.0)            # +0.6% premium
        result = factor_3_basis_anomaly(ms, Direction.LONG)
        assert result is not None
        assert result[1] == -0.15

    def test_long_normal_basis_no_flag(self):
        ms = build_market_state(spot_basis_bps=20.0)            # +0.2%
        assert factor_3_basis_anomaly(ms, Direction.LONG) is None

    def test_short_with_discount_flagged(self):
        ms = build_market_state(spot_basis_bps=-60.0)
        result = factor_3_basis_anomaly(ms, Direction.SHORT)
        assert result is not None


class TestFactor4BtcSystemicImpulse:
    def test_btc_drop_blocks_long(self):
        ms = build_market_state(btc_1m_return=-0.015)
        result = factor_4_btc_systemic_impulse(ms, Direction.LONG)
        assert result is not None
        assert result[1] == -0.20

    def test_btc_pump_blocks_short(self):
        ms = build_market_state(btc_1m_return=+0.015)
        result = factor_4_btc_systemic_impulse(ms, Direction.SHORT)
        assert result is not None

    def test_small_btc_move_no_flag(self):
        ms = build_market_state(btc_1m_return=+0.005)
        assert factor_4_btc_systemic_impulse(ms, Direction.LONG) is None


class TestFactor5BtcEthCorr:
    def test_corr_breakdown_flagged(self):
        ms = build_market_state(btc_eth_corr=0.4)
        result = factor_5_btc_eth_corr_breakdown(ms)
        assert result is not None
        assert result[1] == -0.10

    def test_high_corr_no_flag(self):
        ms = build_market_state(btc_eth_corr=0.9)
        assert factor_5_btc_eth_corr_breakdown(ms) is None


class TestFactor6OISpike:
    def test_long_with_oi_spike_and_dump_flagged(self):
        # We need price_negative — manipulate prev_close < last_close
        ms = build_market_state(oi_growth_24h=20.0)
        # Force prev_close < last_close
        ms.prev_close_1m = ms.last_close_1m - 1.0
        result = factor_6_oi_spike_negative_price(ms, Direction.LONG)
        assert result is not None

    def test_oi_low_no_flag(self):
        ms = build_market_state(oi_growth_24h=5.0)
        assert factor_6_oi_spike_negative_price(ms, Direction.LONG) is None


class TestFactor7BvolExtreme:
    def test_bvol_high_flagged(self):
        ms = build_market_state(bvol=85.0)
        result = factor_7_bvol_extreme(ms)
        assert result is not None
        assert result[1] == -0.10

    def test_bvol_normal_no_flag(self):
        ms = build_market_state(bvol=45.0)
        assert factor_7_bvol_extreme(ms) is None


class TestFactor8Microstructure:
    def test_wide_spread_flagged(self):
        ms = build_market_state(spread_bps=15.0)
        result = factor_8_microstructure_degraded(ms)
        assert result is not None
        assert result[1] == -0.20

    def test_tight_spread_no_flag(self):
        ms = build_market_state(spread_bps=2.0)
        assert factor_8_microstructure_degraded(ms) is None


class TestFactor9FundingSettlement:
    def test_close_to_settlement_flagged(self):
        ms = build_market_state(seconds_to_funding=900)
        result = factor_9_funding_settlement_window(ms)
        assert result is not None
        assert result[1] == -0.10

    def test_far_from_settlement_no_flag(self):
        ms = build_market_state(seconds_to_funding=14000)
        assert factor_9_funding_settlement_window(ms) is None

    def test_zero_seconds_no_flag(self):
        # 0 means already settled / unknown — guard against false positive
        ms = build_market_state(seconds_to_funding=0)
        assert factor_9_funding_settlement_window(ms) is None


class TestFactor10CvdBroke:
    def test_cvd_broken_flagged(self):
        result = factor_10_cvd_broke(True)
        assert result is not None
        assert result[1] == -0.30

    def test_cvd_intact_no_flag(self):
        assert factor_10_cvd_broke(False) is None


class TestFactor11DxyLongCrypto:
    def test_dxy_strong_long_flagged(self):
        ms = build_market_state(dxy_intraday=+0.7)
        result = factor_11_dxy_long_crypto(ms, Direction.LONG)
        assert result is not None
        assert result[1] == -0.10

    def test_dxy_strong_short_no_flag(self):
        ms = build_market_state(dxy_intraday=+0.7)
        assert factor_11_dxy_long_crypto(ms, Direction.SHORT) is None

    def test_dxy_calm_no_flag(self):
        ms = build_market_state(dxy_intraday=0.1)
        assert factor_11_dxy_long_crypto(ms, Direction.LONG) is None


class TestFactor12CalendarEvent:
    def test_in_window_flagged(self):
        ms = build_market_state(in_event=True)
        result = factor_12_calendar_event(ms)
        assert result is not None
        assert result[1] == -0.15

    def test_outside_window_no_flag(self):
        ms = build_market_state(in_event=False)
        assert factor_12_calendar_event(ms) is None


# ─────────────────────────────────────────────────────────────────────────────
# Decision matrix tests — combinations
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionMatrix:
    def test_no_flags_passes(self):
        ms = build_market_state()
        qa = build_qa()
        result = evaluate(ms, qa, Direction.LONG)
        assert result.decision == "PASS"
        assert result.flags == ()
        assert result.total_penalty == 0.0
        assert not result.blocked

    def test_single_minor_flag_still_passes(self):
        # F5 alone = -0.10 → PASS (above -0.15 threshold)
        ms = build_market_state(btc_eth_corr=0.4)
        qa = build_qa()
        result = evaluate(ms, qa, Direction.LONG)
        assert result.decision == "PASS"
        assert result.total_penalty == -0.10

    def test_two_flags_triggers_half(self):
        # F1 (-0.20) gives HALF (≤ -0.15)
        ms = build_market_state(funding=+0.0020)
        qa = build_qa()
        result = evaluate(ms, qa, Direction.LONG)
        assert result.decision == "HALF"
        assert result.half_size is True
        assert result.total_penalty == -0.20

    def test_block_at_minus_30(self):
        # F2 alone = -0.30 → exactly at BLOCK threshold
        ms = build_market_state()
        qa = build_qa(s13=True, vix=35.0)
        result = evaluate(ms, qa, Direction.LONG)
        assert result.decision == "BLOCK"
        assert result.blocked is True
        assert result.total_penalty == -0.30

    def test_combination_block(self):
        # F1 (-0.20) + F11 (-0.10) + F12 (-0.15) = -0.45 → BLOCK
        ms = build_market_state(funding=+0.0020, dxy_intraday=+1.0, in_event=True)
        qa = build_qa()
        result = evaluate(ms, qa, Direction.LONG)
        assert result.decision == "BLOCK"
        assert "F1_FUNDING_LONG_CROWDED" in result.flags
        assert "F11_DXY_STRONG_VS_LONG" in result.flags
        assert "F12_CALENDAR_EVENT_WINDOW" in result.flags

    def test_short_direction_independent_of_dxy_long_factor(self):
        # F11 should NOT trigger for SHORT
        ms = build_market_state(dxy_intraday=+1.0, funding=+0.0001, setup="short_sweep")
        qa = build_qa(direction="SHORT")
        result = evaluate(ms, qa, Direction.SHORT)
        assert "F11_DXY_STRONG_VS_LONG" not in result.flags

    def test_flat_direction_pass_no_eval(self):
        ms = build_market_state(funding=+0.0050)               # would normally flag F1
        qa = build_qa()
        result = evaluate(ms, qa, Direction.FLAT)
        assert result.decision == "PASS"
        assert result.flags == ()


class TestCritiqueResultClass:
    def test_from_factors_pass(self):
        result = CritiqueResult.from_factors([])
        assert result.decision == "PASS"
        assert result.total_penalty == 0.0

    def test_from_factors_block_boundary(self):
        result = CritiqueResult.from_factors([("F2", -0.30)])
        assert result.decision == "BLOCK"

    def test_from_factors_half_boundary(self):
        result = CritiqueResult.from_factors([("F3", -0.15)])
        assert result.decision == "HALF"

    def test_from_factors_pass_just_below(self):
        result = CritiqueResult.from_factors([("X", -0.14)])
        assert result.decision == "PASS"


class TestCvdBrokeIntegration:
    def test_cvd_broke_pushes_to_block(self):
        ms = build_market_state()
        qa = build_qa()
        result = evaluate(ms, qa, Direction.LONG, cvd_broke_before_fill=True)
        assert result.decision == "BLOCK"
        assert "F10_CVD_BROKE_PRE_FILL" in result.flags
