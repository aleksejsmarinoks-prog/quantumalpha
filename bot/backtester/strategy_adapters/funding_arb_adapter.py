"""
Funding-Arb adapter for backtest.

Canonical funding-rate edge:
  - Enter SHORT perp when funding > +open_threshold (collect funding paid by longs)
  - Enter LONG perp when funding < -open_threshold (receive paid funding)
  - Exit when |funding| drops below close_threshold

In production the strategy hedges with a delta-neutral spot leg via the Earn
buffer. For Phase 6.3 backtest we simulate ONLY the perp leg, with no
hedge — this OVERSTATES risk and is a conservative test (real PnL should be
better thanks to spot hedge). Note documented in README.

PRODUCTION INTERFACE NOTE
-------------------------
Same caveat as MeanReversionAdapter: this implements canonical logic
parameterised to match production. To replace with direct production
strategy call, wire `from bot.strategies.funding_arb import FundingArbStrategy`
and an `evaluate_on_history(history, funding_history, params)` method.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..models import Signal, SignalAction, Side
from ..replay_engine import SnapshotContext
from .base_adapter import BaseAdapter


DEFAULT_PARAMS = {
    "open_threshold_8h": 0.0004,        # 0.04% per 8h ≈ +0.12% per day
    "close_threshold_8h": 0.00015,      # exit when funding decays
    "size_usd": 200.0,                  # 20% of $1k or 40% of satellite
}


class FundingArbAdapter(BaseAdapter):
    """
    Collect adverse-side funding by going against the crowd. No hedge in
    backtest (perp-only). Evaluation every 15 minutes (production cadence).
    """

    name = "funding_arb_v1"

    def reset(self, params: dict) -> None:
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        self._params = merged

    def required_lookback_bars(self) -> int:
        return 3                        # very small — funding logic doesn't need long history

    def evaluation_interval(self) -> timedelta:
        return timedelta(minutes=15)

    def evaluate(self, ctx: SnapshotContext) -> Optional[Signal]:
        rate = ctx.latest_funding_rate()
        open_thr = float(self._params["open_threshold_8h"])
        close_thr = float(self._params["close_threshold_8h"])
        size = float(self._params["size_usd"])

        if ctx.open_position_usd > 0:
            # In a position — consider EXIT when funding decays toward zero
            if abs(rate) < close_thr:
                return Signal(
                    timestamp=ctx.now,
                    symbol=ctx.symbol,
                    action=SignalAction.EXIT,
                    size_usd=ctx.open_position_usd,
                )
            # Also exit on sign flip (extreme case)
            if ctx.open_position_side == Side.SELL and rate < -close_thr:
                return Signal(timestamp=ctx.now, symbol=ctx.symbol, action=SignalAction.EXIT,
                              size_usd=ctx.open_position_usd)
            if ctx.open_position_side == Side.BUY and rate > +close_thr:
                return Signal(timestamp=ctx.now, symbol=ctx.symbol, action=SignalAction.EXIT,
                              size_usd=ctx.open_position_usd)
            return None

        # Flat — consider entry
        if rate > +open_thr:
            # Longs paying — short the perp to collect funding
            return Signal(
                timestamp=ctx.now,
                symbol=ctx.symbol,
                action=SignalAction.ENTER_SHORT,
                size_usd=size,
                metadata={"maker": True, "taker_fallback": True, "funding": rate},
            )
        if rate < -open_thr:
            # Shorts paying — long the perp to collect funding
            return Signal(
                timestamp=ctx.now,
                symbol=ctx.symbol,
                action=SignalAction.ENTER_LONG,
                size_usd=size,
                metadata={"maker": True, "taker_fallback": True, "funding": rate},
            )
        return None
