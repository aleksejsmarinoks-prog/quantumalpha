"""
Mean-Reversion adapter for backtest.

PRODUCTION INTERFACE NOTE
-------------------------
The live MeanReversionStrategy on the VPS uses a private method signature
that this adapter cannot import without VPS access. This adapter implements
a **canonical mean-reversion logic** (z-score on rolling close), parameterised
identically to the production class, so backtest behaviour stays representative.

When this adapter is deployed to the VPS alongside the production strategy,
update the `evaluate()` body to call the actual strategy method. Currently
the production class is reachable via:

    from bot.strategies.mean_reversion import MeanReversionStrategy
    self._live = MeanReversionStrategy(capital_pct=..., enabled=True)
    signal = self._live.evaluate_on_history(ctx.history, params=self._params)

If the live class doesn't yet expose `evaluate_on_history`, that bridge
method can be added in ~10 LOC. Pattern is described in DEPLOY_NOTES.md.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pandas as pd

from ..models import Signal, SignalAction
from ..replay_engine import SnapshotContext
from .base_adapter import BaseAdapter


# Default param grid (acceptable per Phase 6.3 prompt default if no answer in 24h)
DEFAULT_PARAMS = {
    "lookback_bars": 20,                # rolling z-score window
    "z_entry": 2.0,                     # enter when |z| > z_entry
    "z_exit": 0.5,                      # exit when |z| < z_exit
    "size_usd": 200.0,                  # 20% of $1000 capital
}


class MeanReversionAdapter(BaseAdapter):
    """
    Canonical z-score reversal:
      z = (close - rolling_mean) / rolling_std
      z < -z_entry → ENTER_LONG    (price below band → buy mean reversion)
      z > +z_entry → ENTER_SHORT   (price above band → sell)
      |z| < z_exit (while in position) → EXIT
    """

    name = "mean_reversion_v1"

    def reset(self, params: dict) -> None:
        merged: dict = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        self._params = merged

    def required_lookback_bars(self) -> int:
        return int(self._params.get("lookback_bars", DEFAULT_PARAMS["lookback_bars"])) + 5

    def evaluation_interval(self) -> timedelta:
        return timedelta(minutes=5)

    def evaluate(self, ctx: SnapshotContext) -> Optional[Signal]:
        if ctx.history.empty:
            return None
        lookback = int(self._params["lookback_bars"])
        z_entry = float(self._params["z_entry"])
        z_exit = float(self._params["z_exit"])
        size = float(self._params["size_usd"])

        closes = ctx.history["close"]
        if len(closes) < lookback + 1:
            return None
        window = closes.iloc[-lookback:]
        mean = window.mean()
        std = window.std()
        if std == 0 or pd.isna(std):
            return None
        last_close = float(closes.iloc[-1])
        z = (last_close - mean) / std

        # Decide
        if ctx.open_position_usd > 0:
            # Already in a position — only consider EXIT
            if abs(z) < z_exit:
                return Signal(
                    timestamp=ctx.now,
                    symbol=ctx.symbol,
                    action=SignalAction.EXIT,
                    size_usd=ctx.open_position_usd,
                )
            return None

        # Flat — consider entry
        if z < -z_entry:
            return Signal(
                timestamp=ctx.now,
                symbol=ctx.symbol,
                action=SignalAction.ENTER_LONG,
                size_usd=size,
                metadata={"maker": True, "taker_fallback": True, "z": z},
            )
        if z > +z_entry:
            return Signal(
                timestamp=ctx.now,
                symbol=ctx.symbol,
                action=SignalAction.ENTER_SHORT,
                size_usd=size,
                metadata={"maker": True, "taker_fallback": True, "z": z},
            )
        return None
