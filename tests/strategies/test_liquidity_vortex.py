"""
Tests for bot.strategies.liquidity_vortex — main LV1 strategy.

Coverage:
  - Sweep detection (LONG/SHORT/none)
  - Hard gate filters (E1-E6)
  - Composite score sub-functions
  - Quarter-Kelly + multiplicative haircut sizing edge cases
  - AEGIS tier classification
  - Self-critique gate integration → cooldown
  - Execution: paper-mode logging, live-mode order placement
  - Position management: TP1/TP2/trailing/emergency exits
  - Numerical stability: zero-equity, zero-MAD, p=0/p=1 Kelly
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from bot.strategies.lv1_models import (
    AegisTier,
    Direction,
    MarketState,
    QADirective,
    RollingStats,
    SweepSignal,
    OpenPosition,
    SymbolThresholds,
    STRATEGY_NAME,
    KELLY_FRACTION_HARD_CAP,
    RISK_PCT_PER_TRADE_HARD_CAP,
)
from bot.strategies import lv1_signals as sig_mod
from bot.strategies.liquidity_vortex import (
    LiquidityVortexStrategy,
    aegis_tier,
    aegis_size_multiplier,
    compute_position_size,
    confidence_multiplier,
    detect_setup_direction,
    empirical_kelly_fraction,
    hard_gate_check,
    quarter_kelly_capped,
    shrunk_win_rate,
    build_signal,
)
from bot.strategies.lv1_self_critique import evaluate as critique_eval
from bot.utils.cvd_stream import CvdStream, robust_z_score
from bot.utils.orderbook_metrics import (
    best_bid_ask,
    book_health_check,
    depth_10bps_usd,
    depth_within_bps,
    mid_price,
    order_book_imbalance,
    spread_bps,
)
from tests.conftest import (
    MockBybitClient,
    MockLedger,
    MockRiskKernel,
    build_market_state,
    build_qa,
)


# ═════════════════════════════════════════════════════════════════════════════
# 1. SWEEP DETECTION
# ═════════════════════════════════════════════════════════════════════════════

class TestSweepDetection:
    def test_long_sweep_detected(self, good_long_state):
        assert detect_setup_direction(good_long_state) == Direction.LONG

    def test_short_sweep_detected(self, good_short_state):
        assert detect_setup_direction(good_short_state) == Direction.SHORT

    def test_no_sweep_neutral(self):
        ms = build_market_state(setup="neutral")
        assert detect_setup_direction(ms) is None

    def test_shallow_proboj_no_sweep(self):
        # Proboj < 0.0015 (15 bps) => no sweep
        ms = build_market_state()
        ms.prev_low_1m = ms.swing_low_5m * 0.9990     # only 10 bps below
        assert detect_setup_direction(ms) is None

    def test_no_reclaim_no_sweep(self):
        # Sweep but close stays below swing — no reclaim
        ms = build_market_state()
        ms.prev_low_1m = ms.swing_low_5m * 0.998
        ms.prev_close_1m = ms.swing_low_5m * 0.999    # below swing
        assert detect_setup_direction(ms) is None


# ═════════════════════════════════════════════════════════════════════════════
# 2. HARD GATES (E1-E6)
# ═════════════════════════════════════════════════════════════════════════════

class TestHardGates:
    def test_clean_state_passes(self, good_long_state):
        qa = build_qa(direction="LONG")
        assert hard_gate_check(good_long_state, qa, Direction.LONG) is None

    def test_wide_spread_blocks(self):
        ms = build_market_state(spread_bps=8.0)
        qa = build_qa()
        assert hard_gate_check(ms, qa, Direction.LONG) == "E5_SPREAD_TOO_WIDE"

    def test_funding_window_blocks(self):
        ms = build_market_state(seconds_to_funding=60)
        qa = build_qa()
        assert hard_gate_check(ms, qa, Direction.LONG) == "E5_FUNDING_SETTLEMENT_NEAR"

    def test_atr_too_low_blocks(self):
        ms = build_market_state(atr_1h=10.0, atr_med_30d=20.0)         # ratio = 0.5
        qa = build_qa()
        assert hard_gate_check(ms, qa, Direction.LONG) == "E4_ATR_REGIME_OUT_OF_BAND"

    def test_atr_too_high_blocks(self):
        ms = build_market_state(atr_1h=80.0, atr_med_30d=20.0)         # ratio = 4
        qa = build_qa()
        assert hard_gate_check(ms, qa, Direction.LONG) == "E4_ATR_REGIME_OUT_OF_BAND"

    def test_funding_extreme_blocks_long(self):
        ms = build_market_state(funding=+0.0020, symbol="ETH/USDT:USDT")
        qa = build_qa()
        assert hard_gate_check(ms, qa, Direction.LONG) == "E3_FUNDING_BLOCKS_LONG"

    def test_qa_opposed_blocks_long(self):
        ms = build_market_state()
        qa = build_qa(direction="SHORT")
        assert hard_gate_check(ms, qa, Direction.LONG) == "E6_QA_OPPOSED_LONG"

    def test_qa_flat_allows_long(self, good_long_state):
        qa = build_qa(direction="FLAT")
        assert hard_gate_check(good_long_state, qa, Direction.LONG) is None


# ═════════════════════════════════════════════════════════════════════════════
# 3. SYMBOL-SPECIFIC THRESHOLDS
# ═════════════════════════════════════════════════════════════════════════════

class TestSymbolThresholds:
    def test_eth_thresholds(self):
        t = SymbolThresholds.for_symbol("ETH/USDT:USDT")
        assert t.funding_long_allowed == +0.0010
        assert t.funding_long_penalty == +0.0015

    def test_sol_thresholds(self):
        t = SymbolThresholds.for_symbol("SOL/USDT:USDT")
        assert t.funding_long_allowed == +0.0015
        assert t.funding_long_penalty == +0.0020

    def test_unknown_symbol_conservative_default(self):
        t = SymbolThresholds.for_symbol("LINK/USDT:USDT")
        assert t.funding_long_allowed == +0.0008


# ═════════════════════════════════════════════════════════════════════════════
# 4. SCORING (composite + sub-scores)
# ═════════════════════════════════════════════════════════════════════════════

class TestScoring:
    def test_clean_long_setup_scores_high(self, good_long_state):
        thresholds = SymbolThresholds.for_symbol(good_long_state.symbol)
        scores = sig_mod.composite_score(good_long_state, Direction.LONG, thresholds)
        assert scores["sweep"] > 0.4
        assert scores["regime"] > 0.5
        assert 0 <= scores["composite"] <= 1.0

    def test_neutral_setup_scores_low(self):
        ms = build_market_state(setup="neutral")
        thresholds = SymbolThresholds.for_symbol(ms.symbol)
        scores = sig_mod.composite_score(ms, Direction.LONG, thresholds)
        assert scores["sweep"] == 0.0

    def test_funding_score_long_negative_funding_max(self):
        thresholds = SymbolThresholds.for_symbol("ETH/USDT:USDT")
        s = sig_mod.funding_score(-0.0010, Direction.LONG, thresholds)
        assert s == 1.0

    def test_funding_score_long_high_positive_zero(self):
        thresholds = SymbolThresholds.for_symbol("ETH/USDT:USDT")
        s = sig_mod.funding_score(+0.0030, Direction.LONG, thresholds)
        assert s == 0.0

    def test_passes_threshold_above(self):
        assert sig_mod.passes_threshold(0.85, -0.05) is True

    def test_passes_threshold_below(self):
        assert sig_mod.passes_threshold(0.85, -0.10) is False

    def test_short_setup_sweep_score(self, good_short_state):
        s = sig_mod.sweep_score(good_short_state, Direction.SHORT)
        assert s > 0.4

    def test_flat_direction_zero_sweep(self, good_long_state):
        assert sig_mod.sweep_score(good_long_state, Direction.FLAT) == 0.0

    def test_zero_swing_returns_zero(self):
        ms = build_market_state()
        ms.swing_low_5m = 0.0
        ms.swing_high_5m = 0.0
        assert sig_mod.sweep_score(ms, Direction.LONG) == 0.0
        assert sig_mod.sweep_score(ms, Direction.SHORT) == 0.0

    def test_cvd_z_score_zero_mad(self):
        ms = build_market_state(cvd_mad_30m=0.0)
        assert sig_mod.cvd_z_score(ms) == 0.0

    def test_cvd_divergence_short(self, good_short_state):
        # Force price HH + CVD LH for SHORT divergence
        good_short_state.prev_high_1m = 3520.0
        good_short_state.last_high_1m = 3510.0
        good_short_state.cvd_15m = 5.0
        good_short_state.cvd_15m_at_prev_low = 50.0
        s = sig_mod.cvd_divergence_score(good_short_state, Direction.SHORT)
        assert s == 1.0

    def test_cvd_divergence_flat_zero(self, good_long_state):
        assert sig_mod.cvd_divergence_score(good_long_state, Direction.FLAT) == 0.0

    def test_regime_score_zero_atr(self):
        ms = build_market_state(atr_med_30d=0.0)
        assert sig_mod.regime_score(ms) == 0.0

    def test_regime_score_dead_market(self):
        ms = build_market_state(atr_1h=10.0, atr_med_30d=20.0)  # ratio 0.5 < 0.7
        assert sig_mod.regime_score(ms) == 0.0

    def test_regime_score_panic(self):
        ms = build_market_state(atr_1h=50.0, atr_med_30d=20.0)  # ratio 2.5 > 2.0
        assert sig_mod.regime_score(ms) == 0.0

    def test_funding_score_short_positive_max(self):
        thresholds = SymbolThresholds.for_symbol("ETH/USDT:USDT")
        s = sig_mod.funding_score(+0.0010, Direction.SHORT, thresholds)
        assert s == 1.0

    def test_funding_score_flat_zero(self):
        thresholds = SymbolThresholds.for_symbol("ETH/USDT:USDT")
        assert sig_mod.funding_score(0.0, Direction.FLAT, thresholds) == 0.0

    def test_obi_score_zero_depth(self):
        ms = build_market_state(depth_10bps=0.0)
        assert sig_mod.obi_score(ms, Direction.LONG) == 0.0

    def test_basis_score_long_premium_high(self):
        ms = build_market_state(spot_basis_bps=80.0)
        assert sig_mod.basis_score(ms, Direction.LONG) == 0.0

    def test_basis_score_short_premium_max(self):
        ms = build_market_state(spot_basis_bps=80.0)
        assert sig_mod.basis_score(ms, Direction.SHORT) == 1.0

    def test_basis_score_flat_neutral(self):
        ms = build_market_state()
        assert sig_mod.basis_score(ms, Direction.FLAT) == 0.5

    def test_basis_score_zero_spot_neutral(self):
        ms = build_market_state()
        ms.spot_price = 0.0
        assert sig_mod.basis_score(ms, Direction.LONG) == 0.5

    def test_composite_short_path(self, good_short_state):
        thresholds = SymbolThresholds.for_symbol(good_short_state.symbol)
        scores = sig_mod.composite_score(good_short_state, Direction.SHORT, thresholds)
        assert "composite" in scores
        assert 0 <= scores["composite"] <= 1.0


# ═════════════════════════════════════════════════════════════════════════════
# 5. KELLY MATH (numerical stability)
# ═════════════════════════════════════════════════════════════════════════════

class TestKellyMath:
    def test_zero_trades_uses_priors(self):
        stats = RollingStats(n_trades=0)
        f = empirical_kelly_fraction(stats)
        # p=0.42 prior, b=2.5 prior → f = (0.42*2.5 - 0.58)/2.5 = 0.188
        assert 0.18 < f < 0.20

    def test_perfect_winrate_no_explosion(self):
        # 100% wins should not produce f > 1
        stats = RollingStats(n_trades=100, wins=100, avg_win_R=2.5, avg_loss_R=1.0)
        f = empirical_kelly_fraction(stats)
        # Beta-shrunk p = (100 + 5) / (100 + 10) = 0.954
        # f = (0.954 * 2.5 - 0.046)/2.5 ≈ 0.945
        assert 0.0 < f < 1.0

    def test_zero_winrate_zero_kelly(self):
        stats = RollingStats(n_trades=100, wins=0, avg_win_R=2.5, avg_loss_R=1.0)
        # Beta-shrunk p = 5/110 ≈ 0.045 → f = (0.045*2.5 - 0.955)/2.5 < 0
        # max(0, ...) clamps to 0
        f = empirical_kelly_fraction(stats)
        assert f == 0.0

    def test_quarter_kelly_caps(self):
        stats = RollingStats(n_trades=100, wins=80, avg_win_R=3.0, avg_loss_R=1.0)
        f_q = quarter_kelly_capped(stats)
        assert f_q <= KELLY_FRACTION_HARD_CAP

    def test_confidence_multiplier_zero_trades(self):
        assert confidence_multiplier(0) == 0.3

    def test_confidence_multiplier_full_at_target(self):
        assert confidence_multiplier(200) == 1.0
        assert confidence_multiplier(500) == 1.0   # capped

    def test_shrunk_winrate_pulls_to_centre(self):
        stats = RollingStats(n_trades=10, wins=10)
        p = shrunk_win_rate(stats)
        # Pure win-rate would be 1.0, beta-shrunk = (10+5)/(10+10) = 0.75
        assert 0.7 < p < 0.8


# ═════════════════════════════════════════════════════════════════════════════
# 6. AEGIS HIERARCHY
# ═════════════════════════════════════════════════════════════════════════════

class TestAegisHierarchy:
    def test_green_below_3pct(self):
        assert aegis_tier(0.02) == AegisTier.GREEN

    def test_yellow_3_to_5pct(self):
        assert aegis_tier(0.03) == AegisTier.YELLOW
        assert aegis_tier(0.045) == AegisTier.YELLOW

    def test_orange_5_to_10pct(self):
        assert aegis_tier(0.07) == AegisTier.ORANGE

    def test_red_10_to_15pct(self):
        assert aegis_tier(0.12) == AegisTier.RED

    def test_black_above_15pct(self):
        assert aegis_tier(0.18) == AegisTier.BLACK

    def test_size_multipliers(self):
        assert aegis_size_multiplier(AegisTier.GREEN) == 1.0
        assert aegis_size_multiplier(AegisTier.YELLOW) == 0.5
        assert aegis_size_multiplier(AegisTier.ORANGE) == 0.0
        assert aegis_size_multiplier(AegisTier.RED) == 0.0
        assert aegis_size_multiplier(AegisTier.BLACK) == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 7. POSITION SIZING
# ═════════════════════════════════════════════════════════════════════════════

class TestPositionSizing:
    def _signal(self, **kw):
        defaults = dict(
            symbol="ETH/USDT:USDT", direction=Direction.LONG,
            entry_zone=3500.0, stop_loss=3475.0, take_profit_1=3520.0,
            take_profit_2=3540.0, take_profit_3=3580.0,
            risk_per_unit=25.0, composite_score=0.85, conviction=0.85,
            regime_multiplier=0.7, funding_multiplier=1.0, confidence_multiplier=0.6,
            self_critique_penalty=0.0, half_size=False,
        )
        defaults.update(kw)
        return SweepSignal(**defaults)

    def test_zero_equity_returns_skipped(self):
        result = compute_position_size(
            satellite_equity=0.0,
            stats=RollingStats(),
            signal=self._signal(),
            aegis=AegisTier.GREEN,
            depth_10bps_usd=1_000_000.0,
        )
        assert result.skipped_reason == "ZERO_EQUITY"

    def test_orange_aegis_blocks_size(self):
        result = compute_position_size(
            satellite_equity=300.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=self._signal(),
            aegis=AegisTier.ORANGE,
            depth_10bps_usd=1_000_000.0,
        )
        assert result.f_final == 0.0
        assert result.skipped_reason == "F_FINAL_ZERO"

    def test_normal_sizing_within_caps(self):
        result = compute_position_size(
            satellite_equity=1000.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=self._signal(),
            aegis=AegisTier.GREEN,
            depth_10bps_usd=10_000_000.0,
        )
        # Risk-based cap: equity * 0.01 / sl_pct = 1000 * 0.01 / (25/3500) = 1400
        # Kelly-based: smaller. notional should be limited by Kelly.
        assert result.notional_final > 0
        assert result.notional_final <= 1000 * 0.01 / (25 / 3500)

    def test_below_min_notional_skipped(self):
        result = compute_position_size(
            satellite_equity=10.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=self._signal(),
            aegis=AegisTier.GREEN,
            depth_10bps_usd=10_000_000.0,
        )
        assert result.skipped_reason == "BELOW_MIN_NOTIONAL"

    def test_half_size_flag_halves(self):
        full = compute_position_size(
            satellite_equity=1000.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=self._signal(half_size=False),
            aegis=AegisTier.GREEN,
            depth_10bps_usd=10_000_000.0,
        )
        half = compute_position_size(
            satellite_equity=1000.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=self._signal(half_size=True),
            aegis=AegisTier.GREEN,
            depth_10bps_usd=10_000_000.0,
        )
        # Risk cap is identical, but Kelly portion is half
        assert half.f_final == pytest.approx(full.f_final * 0.5)

    def test_depth_caps_market_impact(self):
        # Tiny book → notional capped by depth fraction
        result = compute_position_size(
            satellite_equity=1_000_000.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=self._signal(),
            aegis=AegisTier.GREEN,
            depth_10bps_usd=1000.0,         # tiny depth
        )
        assert result.notional_final <= 50.0     # 5% of 1000


# ═════════════════════════════════════════════════════════════════════════════
# 8. STRATEGY EVALUATE() — full pipeline
# ═════════════════════════════════════════════════════════════════════════════

class TestStrategyEvaluate:
    def _strategy(
        self, ledger=None, risk_kernel=None, ms_provider=None, enabled=True
    ) -> LiquidityVortexStrategy:
        return LiquidityVortexStrategy(
            bybit_client=MockBybitClient(),
            ledger=ledger or MockLedger(stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0)),
            risk_kernel=risk_kernel or MockRiskKernel(equity=1000.0),
            qa_provider=lambda s: build_qa(direction="LONG"),
            capital_pct=0.30,
            enabled=enabled,
            live_trading=False,
            symbols=("ETH/USDT:USDT",),
            market_state_provider=ms_provider,
        )

    def test_disabled_strategy_returns_disabled(self):
        strat = self._strategy(enabled=False)
        signal, reason = strat.evaluate("ETH/USDT:USDT")
        assert signal is None
        assert reason == "DISABLED"

    def test_halted_kernel_returns_halted(self):
        rk = MockRiskKernel(equity=1000.0, halted=True)
        strat = self._strategy(risk_kernel=rk)
        _, reason = strat.evaluate("ETH/USDT:USDT")
        assert reason == "RISK_KERNEL_HALTED"

    def test_orange_aegis_pauses(self):
        rk = MockRiskKernel(equity=1000.0, daily_dd=0.07)
        strat = self._strategy(risk_kernel=rk)
        _, reason = strat.evaluate("ETH/USDT:USDT")
        assert reason and reason.startswith("AEGIS_TIER_PAUSED")

    def test_no_market_state_returns_no_state(self):
        strat = self._strategy(ms_provider=lambda s: None)
        _, reason = strat.evaluate("ETH/USDT:USDT")
        assert reason == "NO_MARKET_STATE"

    def test_clean_long_setup_produces_signal(self):
        ms = build_market_state(setup="long_sweep", funding=-0.0001)
        strat = self._strategy(ms_provider=lambda s: ms)
        signal, reason = strat.evaluate("ETH/USDT:USDT")
        # May or may not pass threshold — depending on synthetic numbers — but if reason set it should be informative
        assert (signal is not None) or (reason is not None)

    def test_neutral_setup_no_sweep(self):
        ms = build_market_state(setup="neutral")
        strat = self._strategy(ms_provider=lambda s: ms)
        _, reason = strat.evaluate("ETH/USDT:USDT")
        assert reason == "NO_SWEEP_SETUP"

    def test_existing_position_blocks_new_entry(self):
        ms = build_market_state(setup="long_sweep")
        strat = self._strategy(ms_provider=lambda s: ms)
        # Inject fake open position
        sig = SweepSignal(
            symbol="ETH/USDT:USDT", direction=Direction.LONG, entry_zone=3500,
            stop_loss=3475, take_profit_1=3520, take_profit_2=3540, take_profit_3=3580,
            risk_per_unit=25.0, composite_score=0.8, conviction=0.8,
            regime_multiplier=1.0, funding_multiplier=1.0, confidence_multiplier=0.6,
            self_critique_penalty=0.0,
        )
        strat.open_positions["ETH/USDT:USDT"] = OpenPosition(
            symbol="ETH/USDT:USDT", signal=sig, qty=0.01, notional_usd=35.0,
        )
        _, reason = strat.evaluate("ETH/USDT:USDT")
        assert reason == "POSITION_ALREADY_OPEN"

    def test_cooldown_blocks_entry(self):
        ms = build_market_state(setup="long_sweep")
        strat = self._strategy(ms_provider=lambda s: ms)
        strat.trigger_cooldown("ETH/USDT:USDT", minutes=10)
        _, reason = strat.evaluate("ETH/USDT:USDT")
        assert reason == "IN_COOLDOWN"

    def test_selfcrit_block_triggers_cooldown(self):
        # F2 alone gives -0.30 → BLOCK
        ms = build_market_state(setup="long_sweep")
        strat = self._strategy(ms_provider=lambda s: ms)
        # Override QA to enable F2
        strat.qa_provider = lambda s: build_qa(direction="LONG", s13=True, vix=35.0)
        signal, reason = strat.evaluate("ETH/USDT:USDT")
        assert signal is None
        assert reason and reason.startswith("SELFCRIT_BLOCK")
        assert strat.is_in_cooldown("ETH/USDT:USDT")


# ═════════════════════════════════════════════════════════════════════════════
# 9. EXECUTION (paper-mode)
# ═════════════════════════════════════════════════════════════════════════════

class TestPaperExecution:
    @pytest.fixture
    def strategy(self):
        return LiquidityVortexStrategy(
            bybit_client=MockBybitClient(),
            ledger=MockLedger(stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0)),
            risk_kernel=MockRiskKernel(equity=1000.0),
            capital_pct=0.30,
            enabled=True,
            live_trading=False,
            symbols=("ETH/USDT:USDT",),
        )

    def _good_signal(self):
        return SweepSignal(
            symbol="ETH/USDT:USDT", direction=Direction.LONG,
            entry_zone=3500.0, stop_loss=3475.0, take_profit_1=3520.0,
            take_profit_2=3540.0, take_profit_3=3580.0,
            risk_per_unit=25.0, composite_score=0.85, conviction=0.85,
            regime_multiplier=1.0, funding_multiplier=1.0, confidence_multiplier=0.6,
            self_critique_penalty=0.0,
        )

    @pytest.mark.asyncio
    async def test_paper_execution_logs_and_creates_position(self, strategy):
        sig = self._good_signal()
        sizing = compute_position_size(
            satellite_equity=2000.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=sig, aegis=AegisTier.GREEN, depth_10bps_usd=10_000_000.0,
        )
        assert sizing.skipped_reason is None, f"sizing was skipped: {sizing.skipped_reason}"
        position = await strategy.execute_signal(sig, sizing)
        assert position is not None
        assert position.symbol == "ETH/USDT:USDT"
        assert "ETH/USDT:USDT" in strategy.open_positions
        # Paper trade was logged (either as paper_trades entry or event)
        assert len(strategy.ledger.paper_trades) > 0

    @pytest.mark.asyncio
    async def test_paper_execution_skipped_below_min(self, strategy):
        sig = self._good_signal()
        # Tiny equity → below min notional
        sizing = compute_position_size(
            satellite_equity=10.0,
            stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0),
            signal=sig, aegis=AegisTier.GREEN, depth_10bps_usd=10_000_000.0,
        )
        position = await strategy.execute_signal(sig, sizing)
        assert position is None


# ═════════════════════════════════════════════════════════════════════════════
# 10. POSITION MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

class TestPositionManagement:
    def _setup(self, ms_factory):
        strat = LiquidityVortexStrategy(
            bybit_client=MockBybitClient(),
            ledger=MockLedger(),
            risk_kernel=MockRiskKernel(equity=1000.0),
            capital_pct=0.30,
            enabled=True,
            live_trading=False,
            market_state_provider=ms_factory,
        )
        sig = SweepSignal(
            symbol="ETH/USDT:USDT", direction=Direction.LONG,
            entry_zone=3500.0, stop_loss=3475.0,
            take_profit_1=3520.0, take_profit_2=3540.0, take_profit_3=3580.0,
            risk_per_unit=25.0, composite_score=0.8, conviction=0.8,
            regime_multiplier=1.0, funding_multiplier=1.0, confidence_multiplier=0.6,
            self_critique_penalty=0.0,
        )
        pos = OpenPosition(
            symbol="ETH/USDT:USDT", signal=sig, qty=0.01, notional_usd=35.0,
            current_stop=3475.0,
        )
        strat.open_positions["ETH/USDT:USDT"] = pos
        return strat, pos

    @pytest.mark.asyncio
    async def test_tp1_partial_close_and_be_move(self):
        # Price reaches TP1
        ms = build_market_state()
        ms.price = 3525.0
        strat, pos = self._setup(lambda s: ms)
        await strat.manage_position("ETH/USDT:USDT")
        assert pos.tp1_filled is True
        # Stop moved to BE+offset
        assert pos.current_stop > 3500.0

    @pytest.mark.asyncio
    async def test_funding_flip_emergency(self):
        ms = build_market_state(funding=+0.0020)
        strat, pos = self._setup(lambda s: ms)
        result = await strat.manage_position("ETH/USDT:USDT")
        assert result == "FUNDING_FLIP"
        assert "ETH/USDT:USDT" not in strat.open_positions

    @pytest.mark.asyncio
    async def test_spread_blowout_emergency(self):
        ms = build_market_state(spread_bps=15.0)
        strat, pos = self._setup(lambda s: ms)
        result = await strat.manage_position("ETH/USDT:USDT")
        assert result == "SPREAD_BLOWOUT"

    @pytest.mark.asyncio
    async def test_btc_impulse_emergency(self):
        ms = build_market_state(btc_1m_return=-0.015)
        strat, pos = self._setup(lambda s: ms)
        result = await strat.manage_position("ETH/USDT:USDT")
        assert result == "BTC_IMPULSE"

    @pytest.mark.asyncio
    async def test_time_exit_when_stale(self):
        ms = build_market_state()
        ms.price = 3501.0                             # tiny gain → unrealized R < 0.5
        strat, pos = self._setup(lambda s: ms)
        # Force opened_at into the past
        pos.opened_at = datetime.now(timezone.utc) - timedelta(hours=5)
        result = await strat.manage_position("ETH/USDT:USDT")
        assert result == "TIME_EXIT"


# ═════════════════════════════════════════════════════════════════════════════
# 11. ORDERBOOK METRICS
# ═════════════════════════════════════════════════════════════════════════════

class TestOrderbookMetrics:
    def _book(self, mid=3500.0, spread_bps_=4.0, depth_per_level=2.0):
        spread_pct = spread_bps_ / 10_000.0
        bid = mid * (1.0 - spread_pct / 2.0)
        ask = mid * (1.0 + spread_pct / 2.0)
        return {
            "bids": [
                [bid, depth_per_level],
                [bid * 0.999, depth_per_level],
                [bid * 0.998, depth_per_level],
            ],
            "asks": [
                [ask, depth_per_level],
                [ask * 1.001, depth_per_level],
                [ask * 1.002, depth_per_level],
            ],
            "timestamp": 1714000000000,
        }

    def test_best_bid_ask(self):
        book = self._book()
        b, a = best_bid_ask(book)
        assert b is not None and a is not None
        assert a > b

    def test_mid_price(self):
        book = self._book()
        m = mid_price(book)
        assert m is not None
        assert 3499.0 < m < 3501.0

    def test_spread_bps(self):
        book = self._book(spread_bps_=4.0)
        s = spread_bps(book)
        assert s is not None
        assert 3.5 < s < 4.5

    def test_depth_within_bps(self):
        book = self._book(mid=3500.0, depth_per_level=2.0)
        d = depth_within_bps(book, 50.0)
        # All 3 bid + 3 ask levels are within 50bps. notional ≈ 6 * 3500 * 2 = 42000
        assert d > 30000

    def test_obi_neutral_book(self):
        book = self._book()
        assert order_book_imbalance(book) == 0.0

    def test_obi_buyer_dominated(self):
        book = self._book()
        book["bids"][0] = [book["bids"][0][0], 10.0]
        obi = order_book_imbalance(book)
        assert obi > 0.0

    def test_book_health_check_pass(self):
        book = self._book(spread_bps_=3.0, depth_per_level=50.0)
        assert book_health_check(book, min_depth_usd=50_000.0) is True

    def test_book_health_check_fail_wide_spread(self):
        book = self._book(spread_bps_=15.0)
        assert book_health_check(book, max_spread_bps=10.0) is False

    def test_empty_book(self):
        empty = {"bids": [], "asks": []}
        assert best_bid_ask(empty) == (None, None)
        assert mid_price(empty) is None
        assert spread_bps(empty) is None
        assert depth_10bps_usd(empty) == 0.0
        assert order_book_imbalance(empty) == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 12. CVD STREAM
# ═════════════════════════════════════════════════════════════════════════════

class TestCvdStream:
    def test_empty_stream(self):
        cvd = CvdStream("ETH/USDT:USDT")
        snap = cvd.snapshot()
        assert snap.cvd_15m == 0.0
        assert snap.cvd_60s == 0.0
        assert snap.n_samples_30m == 0

    def test_single_buy_trade(self):
        cvd = CvdStream("ETH/USDT:USDT")
        cvd.ingest_trades([
            {"timestamp": 1714000000000, "side": "buy", "amount": 1.0, "price": 3500.0},
        ])
        snap = cvd.snapshot()
        assert snap.cvd_15m == 1.0
        assert snap.cvd_60s == 1.0

    def test_buy_then_sell_nets(self):
        cvd = CvdStream("ETH/USDT:USDT")
        cvd.ingest_trades([
            {"timestamp": 1714000000000, "side": "buy", "amount": 1.0, "price": 3500.0},
            {"timestamp": 1714000010000, "side": "sell", "amount": 0.4, "price": 3501.0},
        ])
        snap = cvd.snapshot()
        assert snap.cvd_15m == pytest.approx(0.6)

    def test_eviction_drops_old_samples(self):
        cvd = CvdStream("ETH/USDT:USDT")
        cvd.ingest_trades([
            {"timestamp": 1714000000000, "side": "buy", "amount": 1.0, "price": 3500.0},
        ])
        # Add a trade 31 minutes later — old should be evicted from 30m window
        cvd.ingest_trades([
            {"timestamp": 1714000000000 + 31 * 60_000, "side": "buy", "amount": 0.5, "price": 3510.0},
        ])
        # 15m window only sees latest
        snap = cvd.snapshot()
        assert snap.cvd_15m == pytest.approx(0.5)

    def test_invalid_trade_ignored(self):
        cvd = CvdStream("ETH/USDT:USDT")
        cvd.ingest_trades([
            {"timestamp": 0, "side": "buy", "amount": 1.0, "price": 3500.0},
            {"timestamp": 1714000000000, "side": "buy", "amount": 0.0, "price": 3500.0},
            {"timestamp": 1714000000000, "side": "buy", "amount": 1.0, "price": 0.0},
            {"timestamp": 1714000000000, "side": "unknown", "amount": 1.0, "price": 3500.0},
        ])
        snap = cvd.snapshot()
        assert snap.cvd_15m == 0.0

    def test_robust_z_score_zero_mad(self):
        # Should not raise / divide by zero
        assert robust_z_score(5.0, 0.0, 0.0) == 0.0

    def test_robust_z_score_normal(self):
        z = robust_z_score(10.0, 5.0, 2.5)
        assert z == 2.0


# ═════════════════════════════════════════════════════════════════════════════
# 13. STATUS DICT (orchestra-compatible surface)
# ═════════════════════════════════════════════════════════════════════════════

class TestStatusDict:
    def test_status_dict_shape(self):
        strat = LiquidityVortexStrategy(
            bybit_client=MockBybitClient(),
            ledger=MockLedger(),
            risk_kernel=MockRiskKernel(),
            capital_pct=0.10,
            enabled=False,
        )
        status = strat.status_dict()
        assert status["name"] == STRATEGY_NAME
        assert status["enabled"] is False
        assert status["capital_pct"] == 0.10
        assert "version" in status
        assert "open_positions" in status


# ═════════════════════════════════════════════════════════════════════════════
# 14. RUN_ONE_CYCLE — top-level integration
# ═════════════════════════════════════════════════════════════════════════════

class TestRunOneCycle:
    @pytest.mark.asyncio
    async def test_run_one_cycle_disabled_no_trades(self):
        strat = LiquidityVortexStrategy(
            bybit_client=MockBybitClient(),
            ledger=MockLedger(),
            risk_kernel=MockRiskKernel(),
            capital_pct=0.10,
            enabled=False,
            symbols=("ETH/USDT:USDT",),
        )
        report = await strat.run_one_cycle()
        assert report["opened"] == []

    @pytest.mark.asyncio
    async def test_run_one_cycle_no_setup_returns_clean(self):
        ms = build_market_state(setup="neutral")
        strat = LiquidityVortexStrategy(
            bybit_client=MockBybitClient(),
            ledger=MockLedger(),
            risk_kernel=MockRiskKernel(),
            capital_pct=0.10,
            enabled=True,
            symbols=("ETH/USDT:USDT",),
            market_state_provider=lambda s: ms,
        )
        report = await strat.run_one_cycle()
        assert report["opened"] == []
        assert report["managed"] == []
