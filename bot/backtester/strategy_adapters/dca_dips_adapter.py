"""
DCA-on-dips adapter for backtest.

Logic: ENTER_LONG when last bar's close drops > pct_drop from N-bar high.
Exit when price recovers to entry × (1 + tp_pct) or stop_pct breached.

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..models import Signal, SignalAction
from ..replay_engine import SnapshotContext
from .base_adapter import BaseAdapter


DEFAULT_PARAMS = {
    "lookback_bars": 30,        # high lookback
    "drop_pct": 0.05,           # buy if 5% below rolling high
    "tp_pct": 0.04,             # +4% target
    "stop_pct": 0.06,           # -6% stop
    "size_usd": 100.0,
}


class DcaDipsAdapter(BaseAdapter):
    name = "dca_dips_v1"

    def reset(self, params: dict) -> None:
        merged = dict(DEFAULT_PARAMS)
        merged.update(params or {})
        self._params = merged
        self._entry_price: float | None = None

    def required_lookback_bars(self) -> int:
        return int(self._params["lookback_bars"]) + 2

    def evaluation_interval(self) -> timedelta:
        return timedelta(minutes=5)

    def evaluate(self, ctx: SnapshotContext) -> Optional[Signal]:
        if ctx.history.empty:
            return None
        lookback = int(self._params["lookback_bars"])
        if len(ctx.history) < lookback + 1:
            return None
        window_high = float(ctx.history["high"].iloc[-lookback:].max())
        last_close = float(ctx.history["close"].iloc[-1])
        drop_pct = (window_high - last_close) / window_high if window_high > 0 else 0.0

        if ctx.open_position_usd > 0 and self._entry_price is not None:
            tp_pct = float(self._params["tp_pct"])
            stop_pct = float(self._params["stop_pct"])
            change = (last_close - self._entry_price) / self._entry_price
            if change >= tp_pct or change <= -stop_pct:
                self._entry_price = None
                return Signal(
                    timestamp=ctx.now, symbol=ctx.symbol,
                    action=SignalAction.EXIT, size_usd=ctx.open_position_usd,
                )
            return None

        # Flat — look for dip
        if drop_pct > float(self._params["drop_pct"]):
            self._entry_price = last_close
            return Signal(
                timestamp=ctx.now, symbol=ctx.symbol,
                action=SignalAction.ENTER_LONG,
                size_usd=float(self._params["size_usd"]),
                metadata={"maker": False, "taker_fallback": True},
            )
        return None
