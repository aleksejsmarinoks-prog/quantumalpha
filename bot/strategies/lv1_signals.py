"""
LV1 — Signal Scoring Helpers
============================

Pure functions computing composite signal score from MarketState.

Composite score (blueprint §6.5):
    SignalScore =
      0.30 × SweepScore   (How clean was the liquidity sweep)
    + 0.25 × CVDScore     (Strength of order-flow confirmation)
    + 0.15 × OBIScore     (Orderbook imbalance recovery proxy)
    + 0.15 × RegimeScore  (1H trend alignment + ATR fit)
    + 0.10 × FundingScore (Carry tailwind/headwind)
    + 0.05 × BasisScore   (Spot-perp basis — "Signal #46" idea)
    - SelfCritiquePenalty (computed in lv1_self_critique.py)

Each score returns a float in [0.0, 1.0] before weighting.

Author: QuantForge / QuantumAlpha
Phase: 6.1
"""
from __future__ import annotations

import math
from typing import Optional

from .lv1_models import (
    Direction,
    MarketState,
    SymbolThresholds,
    SIGNAL_SCORE_THRESHOLD,
)


# ─────────────────────────────────────────────────────────────────────────────
# Component scores (each → [0, 1])
# ─────────────────────────────────────────────────────────────────────────────

def sweep_score(ms: MarketState, direction: Direction) -> float:
    """
    Quality of liquidity sweep:
      1.0 if proboj depth ≥ 0.30%, reclaim is clean (close > swing for LONG).
      Decays linearly with shallow proboj.
    """
    if direction == Direction.LONG:
        # We need: prev candle pierced swing_low and closed above it
        if ms.swing_low_5m <= 0:
            return 0.0
        proboj_depth = (ms.swing_low_5m - ms.prev_low_1m) / ms.swing_low_5m
        reclaim_quality = (ms.prev_close_1m - ms.swing_low_5m) / max(ms.swing_low_5m, 1e-9)
        if proboj_depth <= 0 or reclaim_quality <= 0:
            return 0.0
        # Map proboj 0.0015..0.005 → 0.4..1.0
        proboj_norm = max(0.0, min(1.0, (proboj_depth - 0.0015) / 0.0035))
        # Reclaim — should be at least 0.05% above swing
        reclaim_norm = max(0.0, min(1.0, reclaim_quality / 0.001))
        return 0.4 + 0.6 * (0.5 * proboj_norm + 0.5 * reclaim_norm)

    if direction == Direction.SHORT:
        if ms.swing_high_5m <= 0:
            return 0.0
        proboj_depth = (ms.prev_high_1m - ms.swing_high_5m) / ms.swing_high_5m
        reclaim_quality = (ms.swing_high_5m - ms.prev_close_1m) / max(ms.swing_high_5m, 1e-9)
        if proboj_depth <= 0 or reclaim_quality <= 0:
            return 0.0
        proboj_norm = max(0.0, min(1.0, (proboj_depth - 0.0015) / 0.0035))
        reclaim_norm = max(0.0, min(1.0, reclaim_quality / 0.001))
        return 0.4 + 0.6 * (0.5 * proboj_norm + 0.5 * reclaim_norm)

    return 0.0


def cvd_z_score(ms: MarketState) -> float:
    """
    Robust z-score (median + MAD) of cvd_60s vs rolling 30m baseline.
    ChatGPT's contribution from blueprint §5.

    Returns positive z for buying flow excess, negative for selling.
    """
    if ms.cvd_rolling_mad_30m <= 0:
        return 0.0
    return (ms.cvd_60s - ms.cvd_rolling_median_30m) / ms.cvd_rolling_mad_30m


def cvd_divergence_score(ms: MarketState, direction: Direction) -> float:
    """
    For LONG: price made LL while CVD made HL (sellers exhausted).
    For SHORT: price made HH while CVD made LH (buyers exhausted).

    Combined with CVD z-score sign for confirmation strength.
    """
    z = cvd_z_score(ms)

    if direction == Direction.LONG:
        # Price LL: prev_low < lowest of older bars (here: last_low_1m or older)
        price_ll = ms.prev_low_1m < ms.last_low_1m
        cvd_hl = ms.cvd_15m > ms.cvd_15m_at_prev_low
        divergence_classical = 1.0 if (price_ll and cvd_hl) else 0.0
        # ChatGPT alternate: cvd_60s_z < -2 AND slope > 0 → exhaustion
        # (we use z magnitude as proxy)
        z_signal = max(0.0, min(1.0, (-z - 2.0) / 2.0)) if z < -2.0 else 0.0
        return max(divergence_classical, z_signal)

    if direction == Direction.SHORT:
        price_hh = ms.prev_high_1m > ms.last_high_1m
        cvd_lh = ms.cvd_15m < ms.cvd_15m_at_prev_low  # symmetric: lower at prev high
        divergence_classical = 1.0 if (price_hh and cvd_lh) else 0.0
        z_signal = max(0.0, min(1.0, (z - 2.0) / 2.0)) if z > 2.0 else 0.0
        return max(divergence_classical, z_signal)

    return 0.0


def regime_score(ms: MarketState) -> float:
    """
    Volatility-regime fit: peak at atr_ratio=1.2, decay outside [0.7, 2.0].
    """
    if ms.atr_median_30d <= 0:
        return 0.0
    atr_ratio = ms.atr_1h / ms.atr_median_30d
    if atr_ratio <= 0.7 or atr_ratio >= 2.0:
        return 0.0
    # Triangle peak at 1.2
    if atr_ratio <= 1.2:
        return (atr_ratio - 0.7) / 0.5
    return (2.0 - atr_ratio) / 0.8


def funding_score(funding_rate: float, direction: Direction, thresholds: SymbolThresholds) -> float:
    """
    Reward favourable carry, penalise crowded same-side funding.
    LONG benefits from negative funding (paid to hold long).
    """
    if direction == Direction.LONG:
        # Best: very negative funding. Worst: positive funding.
        if funding_rate < -0.0005:
            return 1.0
        if funding_rate < 0.0:
            return 0.8
        if funding_rate < thresholds.funding_long_allowed:
            return 0.6
        if funding_rate < thresholds.funding_long_penalty:
            return 0.3
        return 0.0
    if direction == Direction.SHORT:
        if funding_rate > 0.0005:
            return 1.0
        if funding_rate > 0.0:
            return 0.8
        if funding_rate > thresholds.funding_short_allowed:
            return 0.6
        if funding_rate > thresholds.funding_short_penalty:
            return 0.3
        return 0.0
    return 0.0


def obi_score(ms: MarketState, direction: Direction) -> float:
    """
    Orderbook-imbalance proxy: depth recovery side relative to total.
    Without full L2 reconstruction we use depth_10bps_usd as available signal.

    Higher depth on supportive side → higher score. Conservative default 0.5.
    """
    # Without bid/ask split here we approximate: deep book => liquidity supportive.
    # depth_10bps_usd >= 1M is a healthy threshold.
    if ms.depth_10bps_usd <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(ms.depth_10bps_usd) / 7.0))  # 1e7 USD → 1.0


def basis_score(ms: MarketState, direction: Direction) -> float:
    """
    Spot-perp basis: small basis aligned with trade direction is a tailwind.
    Large basis against trade direction → low score.
    """
    if ms.spot_price <= 0:
        return 0.5
    basis = (ms.price - ms.spot_price) / ms.spot_price  # >0 means perp premium
    if direction == Direction.LONG:
        # LONG benefits from small premium or discount; large premium = late
        if basis < -0.002:
            return 1.0
        if basis < 0.0015:
            return 0.7
        if basis < 0.005:
            return 0.3
        return 0.0
    if direction == Direction.SHORT:
        if basis > 0.002:
            return 1.0
        if basis > -0.0015:
            return 0.7
        if basis > -0.005:
            return 0.3
        return 0.0
    return 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Composite
# ─────────────────────────────────────────────────────────────────────────────

WEIGHTS = {
    "sweep": 0.30,
    "cvd": 0.25,
    "obi": 0.15,
    "regime": 0.15,
    "funding": 0.10,
    "basis": 0.05,
}


def composite_score(
    ms: MarketState,
    direction: Direction,
    thresholds: SymbolThresholds,
) -> dict[str, float]:
    """
    Compute all sub-scores and weighted composite.

    Returns dict with each component plus 'composite' key (pre-self-critique).
    """
    components = {
        "sweep": sweep_score(ms, direction),
        "cvd": cvd_divergence_score(ms, direction),
        "obi": obi_score(ms, direction),
        "regime": regime_score(ms),
        "funding": funding_score(ms.funding_rate, direction, thresholds),
        "basis": basis_score(ms, direction),
    }
    composite = sum(WEIGHTS[k] * v for k, v in components.items())
    return {**components, "composite": composite}


def passes_threshold(score: float, penalty: float) -> bool:
    """
    Final entry decision: composite + penalty must clear blueprint threshold (0.78).
    Penalty is ≤ 0.
    """
    return (score + penalty) >= SIGNAL_SCORE_THRESHOLD
