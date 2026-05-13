"""
QA Backtester — Execution Simulator
====================================

Simulates Bybit perpetuals order execution with realistic slippage and fees.
Used by ReplayEngine to convert signals into Fills.

Slippage model (basis points):
    slippage_bp = (kline_spread_bp / 2) + k * sqrt(size_usd / adv_24h_usd)

    where:
      kline_spread_bp ≈ 10000 * (high - low) / mid    (rough proxy from candle)
      k = 15 (Kyle's lambda calibration, configurable)
      adv_24h_usd = average daily traded notional

Maker fill simulation:
  - probability `maker_fill_rate` (default 0.80)
  - on miss → returns None (caller must retry as taker or abandon)
  - controlled by injected RNG for determinism

Fees:
  - maker 0.02%, taker 0.055% (Bybit non-VIP default)

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .models import Fill, OrderType, Side


log = logging.getLogger("qa.backtester.execution_sim")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MAKER_FEE_BP = 2.0          # 0.02% = 2 bp
TAKER_FEE_BP = 5.5          # 0.055% = 5.5 bp
DEFAULT_KYLE_K = 15.0
DEFAULT_MAKER_FILL_RATE = 0.80


# ─────────────────────────────────────────────────────────────────────────────
# Market snapshot used by simulator
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketSnapshot:
    """
    Minimal view of market at execution time. ReplayEngine builds these from
    historical bars and passes to executor.
    """
    timestamp: datetime
    symbol: str
    mid_price: float
    high: float
    low: float
    adv_24h_usd: float          # for slippage calibration


# ─────────────────────────────────────────────────────────────────────────────
# Slippage / fee helpers (pure)
# ─────────────────────────────────────────────────────────────────────────────

def kline_spread_bp(high: float, low: float, mid: float) -> float:
    """Rough spread proxy: half of high-low range as bps of mid."""
    if mid <= 0:
        return 0.0
    return ((high - low) / mid) * 10_000.0


def compute_slippage_bp(
    snapshot: MarketSnapshot,
    size_usd: float,
    kyle_k: float = DEFAULT_KYLE_K,
) -> float:
    """
    Slippage in basis points using Kyle's lambda approximation.
    Floor at 0 (in case of degenerate inputs).
    """
    if size_usd <= 0 or snapshot.adv_24h_usd <= 0:
        spread_half = kline_spread_bp(snapshot.high, snapshot.low, snapshot.mid_price) / 2.0
        return max(0.0, spread_half)
    spread_half = kline_spread_bp(snapshot.high, snapshot.low, snapshot.mid_price) / 2.0
    impact = kyle_k * math.sqrt(size_usd / snapshot.adv_24h_usd)
    return max(0.0, spread_half + impact)


def compute_fee_usd(size_usd: float, order_type: OrderType) -> float:
    bp = MAKER_FEE_BP if order_type == OrderType.MAKER else TAKER_FEE_BP
    return abs(size_usd) * bp / 10_000.0


def apply_slippage_to_price(mid: float, slippage_bp: float, side: Side) -> float:
    """Buy slips up, sell slips down."""
    factor = slippage_bp / 10_000.0
    return mid * (1.0 + factor) if side == Side.BUY else mid * (1.0 - factor)


# ─────────────────────────────────────────────────────────────────────────────
# Simulator
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionSimulator:
    """
    Stateful executor with a seeded RNG for reproducibility.

    Tests should inject a seeded random.Random; production runs accept seed
    in __init__ or rely on default seed=42.
    """

    def __init__(
        self,
        maker_fill_rate: float = DEFAULT_MAKER_FILL_RATE,
        kyle_k: float = DEFAULT_KYLE_K,
        rng: Optional[random.Random] = None,
        seed: int = 42,
    ):
        if not 0.0 <= maker_fill_rate <= 1.0:
            raise ValueError(f"maker_fill_rate must be in [0, 1], got {maker_fill_rate}")
        self.maker_fill_rate = maker_fill_rate
        self.kyle_k = kyle_k
        self._rng = rng if rng is not None else random.Random(seed)

    def execute_order(
        self,
        snapshot: MarketSnapshot,
        side: Side,
        size_usd: float,
        order_type: OrderType,
    ) -> Optional[Fill]:
        """
        Simulate one order. Returns Fill or None if maker order didn't fill.

        Important semantics:
          - TAKER always fills at slipped price
          - MAKER fills with probability `maker_fill_rate`; on success the price
            is the mid (no taker slippage), but a tiny passive slippage may
            apply for size impact.
        """
        if size_usd <= 0:
            return None
        if snapshot.mid_price <= 0:
            return None

        if order_type == OrderType.MAKER:
            roll = self._rng.random()
            if roll > self.maker_fill_rate:
                log.debug("maker did NOT fill (roll=%.3f > %.2f)", roll, self.maker_fill_rate)
                return None
            # Maker fills near mid with minimal slippage (only size impact)
            impact_bp = max(0.0, self.kyle_k * math.sqrt(size_usd / max(snapshot.adv_24h_usd, 1.0)) / 4.0)
            fill_price = apply_slippage_to_price(snapshot.mid_price, impact_bp, side)
            fee = compute_fee_usd(size_usd, OrderType.MAKER)
            return Fill(
                timestamp=snapshot.timestamp,
                symbol=snapshot.symbol,
                side=side,
                size_usd=size_usd,
                fill_price=fill_price,
                fee_usd=fee,
                slippage_bp=impact_bp,
                order_type=OrderType.MAKER,
            )

        # TAKER
        slip = compute_slippage_bp(snapshot, size_usd, self.kyle_k)
        fill_price = apply_slippage_to_price(snapshot.mid_price, slip, side)
        fee = compute_fee_usd(size_usd, OrderType.TAKER)
        return Fill(
            timestamp=snapshot.timestamp,
            symbol=snapshot.symbol,
            side=side,
            size_usd=size_usd,
            fill_price=fill_price,
            fee_usd=fee,
            slippage_bp=slip,
            order_type=OrderType.TAKER,
        )

    def reset_rng(self, seed: int) -> None:
        """Re-seed for deterministic re-runs."""
        self._rng = random.Random(seed)
