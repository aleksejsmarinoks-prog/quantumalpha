"""
LV1 — SELF_CRITIQUE Gate #45 (12-factor matrix)
================================================

Combined from all 4 LLM sources (blueprint §6.6):

| #  | Factor                                                | Penalty | Source   |
|----|-------------------------------------------------------|---------|----------|
| 1  | Funding crowding (>+0.0015 long / <-0.0010 short)     | -0.20   | All      |
| 2  | S13 active + VIX>30 + LONG direction                   | -0.30   | Claude   |
| 3  | Spot-perp basis anomaly (>0.5%)                        | -0.15   | Claude   |
| 4  | BTC systemic impulse against trade (>1% in 1m)         | -0.20   | ChatGPT  |
| 5  | BTC-ETH correlation breakdown (<0.5)                   | -0.10   | DeepSeek |
| 6  | OI spike (>15% in 24h) + price negative                | -0.15   | Kimi     |
| 7  | BVOL extreme (>80)                                     | -0.10   | Kimi     |
| 8  | Spread > 10 bps OR depth dropped >35% in 500ms         | -0.20   | ChatGPT  |
| 9  | Funding settlement <30 min away                        | -0.10   | Kimi     |
| 10 | CVD confirmation broke before fill                     | -0.30   | ChatGPT  |
| 11 | DXY >+0.5% intraday + LONG crypto                      | -0.10   | Kimi     |
| 12 | Active calendar event window (FOMC/CPI ±15min)         | -0.15   | Kimi     |

Decision matrix:
  - Total penalty ≤ -0.30 → BLOCK trade, cooldown 60 min
  - -0.30 < Total penalty ≤ -0.15 → ALLOW with size × 0.5 (half-size flag)
  - Total penalty > -0.15 → PASS at full size

Each factor returns Optional[tuple[str, float]] — flag name + penalty (negative)
or None if not triggered. Pure functions, fully testable.

Author: QuantForge / QuantumAlpha
Phase: 6.1
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .lv1_models import (
    Direction,
    MarketState,
    QADirective,
    SELFCRIT_BLOCK_PENALTY,
    SELFCRIT_HALVE_PENALTY,
)


# ─────────────────────────────────────────────────────────────────────────────
# Output container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CritiqueResult:
    """Outcome of self-critique evaluation."""
    flags: tuple[str, ...]
    total_penalty: float
    decision: str                # "PASS" | "HALF" | "BLOCK"
    half_size: bool
    blocked: bool

    @staticmethod
    def from_factors(triggered: list[tuple[str, float]]) -> "CritiqueResult":
        flags = tuple(name for name, _ in triggered)
        total = sum(p for _, p in triggered)
        if total <= SELFCRIT_BLOCK_PENALTY:
            return CritiqueResult(flags, total, "BLOCK", False, True)
        if total <= SELFCRIT_HALVE_PENALTY:
            return CritiqueResult(flags, total, "HALF", True, False)
        return CritiqueResult(flags, total, "PASS", False, False)


# ─────────────────────────────────────────────────────────────────────────────
# 12 individual factor checks
# ─────────────────────────────────────────────────────────────────────────────

def factor_1_funding_crowding(ms: MarketState, direction: Direction) -> Optional[tuple[str, float]]:
    """Funding skewed in same direction as planned trade — crowd already there."""
    if direction == Direction.LONG and ms.funding_rate > +0.0015:
        return ("F1_FUNDING_LONG_CROWDED", -0.20)
    if direction == Direction.SHORT and ms.funding_rate < -0.0010:
        return ("F1_FUNDING_SHORT_CROWDED", -0.20)
    return None


def factor_2_s13_long_forbidden(qa: QADirective, direction: Direction) -> Optional[tuple[str, float]]:
    """During S13 forced-liquidation regime + VIX>30, no longs."""
    if qa.s13_active and qa.vix_level > 30.0 and direction == Direction.LONG:
        return ("F2_S13_VIX_LONG_FORBIDDEN", -0.30)
    return None


def factor_3_basis_anomaly(ms: MarketState, direction: Direction) -> Optional[tuple[str, float]]:
    """Spot-perp basis already > 0.5% in adverse direction."""
    if ms.spot_price <= 0:
        return None
    basis = (ms.price - ms.spot_price) / ms.spot_price
    if direction == Direction.LONG and basis > +0.005:
        return ("F3_PERP_PREMIUM_HOT", -0.15)
    if direction == Direction.SHORT and basis < -0.005:
        return ("F3_PERP_DISCOUNT_HOT", -0.15)
    return None


def factor_4_btc_systemic_impulse(ms: MarketState, direction: Direction) -> Optional[tuple[str, float]]:
    """BTC moved >1% in last minute against trade direction."""
    if direction == Direction.LONG and ms.btc_1m_return < -0.01:
        return ("F4_BTC_IMPULSE_DOWN", -0.20)
    if direction == Direction.SHORT and ms.btc_1m_return > +0.01:
        return ("F4_BTC_IMPULSE_UP", -0.20)
    return None


def factor_5_btc_eth_corr_breakdown(ms: MarketState) -> Optional[tuple[str, float]]:
    """BTC-ETH 60d correlation dropped below 0.5 — macro contamination risk."""
    if ms.btc_eth_corr_60d < 0.5:
        return ("F5_BTC_ETH_CORR_BREAKDOWN", -0.10)
    return None


def factor_6_oi_spike_negative_price(
    ms: MarketState, direction: Direction
) -> Optional[tuple[str, float]]:
    """OI grew >15% in 24h with negative price action — squeeze risk."""
    if ms.oi_growth_24h_pct > 15.0:
        # Use prev_close vs last_close as 1m proxy for "price negative"
        price_negative = ms.prev_close_1m < ms.last_close_1m
        if direction == Direction.LONG and price_negative:
            return ("F6_OI_SPIKE_PRICE_DOWN", -0.15)
        if direction == Direction.SHORT and not price_negative:
            return ("F6_OI_SPIKE_PRICE_UP", -0.15)
    return None


def factor_7_bvol_extreme(ms: MarketState) -> Optional[tuple[str, float]]:
    """Implied volatility index extreme (>80)."""
    if ms.bvol_index > 80.0:
        return ("F7_BVOL_EXTREME", -0.10)
    return None


def factor_8_microstructure_degraded(ms: MarketState) -> Optional[tuple[str, float]]:
    """
    Spread blew out OR book depth collapsed.
    We use spread_bps > 10 as primary trigger (depth-drop deltas tracked in book stream).
    """
    if ms.spread_bps > 10.0:
        return ("F8_SPREAD_BLOWOUT", -0.20)
    return None


def factor_9_funding_settlement_window(ms: MarketState) -> Optional[tuple[str, float]]:
    """Funding settlement < 30 minutes away — exposure to fee at fill."""
    if 0 < ms.seconds_to_funding < 1800:
        return ("F9_FUNDING_SETTLEMENT_NEAR", -0.10)
    return None


def factor_10_cvd_broke(cvd_broke_before_fill: bool) -> Optional[tuple[str, float]]:
    """
    CVD divergence inverted between signal generation and order fill.
    State is passed in from execution layer (see liquidity_vortex._evaluate_pre_fill).
    """
    if cvd_broke_before_fill:
        return ("F10_CVD_BROKE_PRE_FILL", -0.30)
    return None


def factor_11_dxy_long_crypto(ms: MarketState, direction: Direction) -> Optional[tuple[str, float]]:
    """DXY +0.5% intraday + LONG crypto = USD strength headwind."""
    if direction == Direction.LONG and ms.dxy_intraday_pct > 0.5:
        return ("F11_DXY_STRONG_VS_LONG", -0.10)
    return None


def factor_12_calendar_event(ms: MarketState) -> Optional[tuple[str, float]]:
    """FOMC/CPI/NFP ±15 min window — event risk."""
    if ms.in_calendar_event_window:
        return ("F12_CALENDAR_EVENT_WINDOW", -0.15)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(
    ms: MarketState,
    qa: QADirective,
    direction: Direction,
    cvd_broke_before_fill: bool = False,
) -> CritiqueResult:
    """
    Run all 12 factors against current state and produce CritiqueResult.

    Args:
        ms: Current market state snapshot.
        qa: QA directive for the asset.
        direction: Planned trade direction (LONG or SHORT).
        cvd_broke_before_fill: True only if called during pre-fill re-check
            and CVD divergence inverted.

    Returns:
        CritiqueResult with flags, total penalty, decision, half_size, blocked.
    """
    if direction == Direction.FLAT:
        return CritiqueResult(tuple(), 0.0, "PASS", False, False)

    triggered: list[tuple[str, float]] = []

    for check in (
        lambda: factor_1_funding_crowding(ms, direction),
        lambda: factor_2_s13_long_forbidden(qa, direction),
        lambda: factor_3_basis_anomaly(ms, direction),
        lambda: factor_4_btc_systemic_impulse(ms, direction),
        lambda: factor_5_btc_eth_corr_breakdown(ms),
        lambda: factor_6_oi_spike_negative_price(ms, direction),
        lambda: factor_7_bvol_extreme(ms),
        lambda: factor_8_microstructure_degraded(ms),
        lambda: factor_9_funding_settlement_window(ms),
        lambda: factor_10_cvd_broke(cvd_broke_before_fill),
        lambda: factor_11_dxy_long_crypto(ms, direction),
        lambda: factor_12_calendar_event(ms),
    ):
        result = check()
        if result is not None:
            triggered.append(result)

    return CritiqueResult.from_factors(triggered)
