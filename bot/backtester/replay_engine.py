"""
QA Backtester — Replay Engine
==============================

Iterates historical Bybit klines chronologically and feeds each tick to a
strategy adapter. Captures signals, simulates fills, tracks open positions
and equity.

CRITICAL: NO LOOKAHEAD BIAS
---------------------------
At time T, the engine exposes ONLY:
  - All bars with timestamp ≤ T (closed)
  - Funding rates with timestamp ≤ T

Strategies CANNOT see future bars. Fills happen at next bar's open price
(not the same bar's close — that would be lookahead).

Author: QuantForge / QuantumAlpha
Phase: 6.3
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

import pandas as pd

from .execution_sim import ExecutionSimulator, MarketSnapshot
from .models import (
    Fill,
    OrderType,
    Side,
    Signal,
    SignalAction,
    Trade,
)


log = logging.getLogger("qa.backtester.replay_engine")


# ─────────────────────────────────────────────────────────────────────────────
# Strategy adapter Protocol
# ─────────────────────────────────────────────────────────────────────────────

class StrategyAdapterProto(Protocol):
    """
    A strategy adapter wraps a live strategy for backtest use.
    Adapter.evaluate() receives a SnapshotContext (current bar + history slice)
    and returns Optional[Signal].
    """

    name: str

    def reset(self, params: dict) -> None: ...

    def evaluate(self, ctx: "SnapshotContext") -> Optional[Signal]: ...

    def required_lookback_bars(self) -> int: ...

    def evaluation_interval(self) -> timedelta: ...


# ─────────────────────────────────────────────────────────────────────────────
# SnapshotContext — what a strategy sees at time T
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SnapshotContext:
    """
    Read-only snapshot at time T. Strategies receive this and must not
    reach into future data.
    """
    now: datetime                     # current backtest time
    symbol: str
    history: pd.DataFrame             # klines from start up to and including the current closed bar
    funding_history: pd.DataFrame     # funding rates up to T
    capital_usd: float
    open_position_usd: float
    open_position_side: Optional[Side]
    adv_24h_usd: float                # rolling ADV estimate

    @property
    def last_close(self) -> float:
        return float(self.history["close"].iloc[-1]) if not self.history.empty else 0.0

    @property
    def last_high(self) -> float:
        return float(self.history["high"].iloc[-1]) if not self.history.empty else 0.0

    @property
    def last_low(self) -> float:
        return float(self.history["low"].iloc[-1]) if not self.history.empty else 0.0

    def latest_funding_rate(self) -> float:
        if self.funding_history.empty:
            return 0.0
        return float(self.funding_history["funding_rate"].iloc[-1])


# ─────────────────────────────────────────────────────────────────────────────
# ReplayEngine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _OpenPosition:
    symbol: str
    side: Side
    size_usd: float
    entry_fill: Fill
    last_funding_settle_ms: int = 0


class ReplayEngine:
    """
    Replay historical data chronologically. Holds at most one position per
    symbol at a time (no scale-ins for backtest simplicity).
    """

    def __init__(
        self,
        adapter: StrategyAdapterProto,
        klines: dict[str, pd.DataFrame],          # {symbol: df_5m}
        funding: dict[str, pd.DataFrame],         # {symbol: df_funding} (may be empty)
        execution_sim: Optional[ExecutionSimulator] = None,
        starting_capital_usd: float = 1000.0,
    ):
        self.adapter = adapter
        self.klines = klines
        self.funding = funding
        self.exec_sim = execution_sim or ExecutionSimulator()
        self.starting_capital = starting_capital_usd

    # ── ADV estimate (rolling 24h notional) ──────────────────────────────
    @staticmethod
    def _adv_24h_usd(df: pd.DataFrame, ts: datetime) -> float:
        if df.empty:
            return 0.0
        cutoff = ts - timedelta(hours=24)
        sub = df[(df.index <= ts) & (df.index >= cutoff)]
        if sub.empty:
            return 0.0
        notional = (sub["close"] * sub["volume"]).sum()
        return float(notional)

    # ── Funding accrual ──────────────────────────────────────────────────
    @staticmethod
    def _accumulate_funding_pnl(
        funding_df: pd.DataFrame,
        position: _OpenPosition,
        from_ts: datetime,
        to_ts: datetime,
    ) -> float:
        """
        Sum funding payments to/from position between two timestamps.
        Longs pay positive funding, shorts receive it. Rates assumed 8h.
        """
        if funding_df.empty:
            return 0.0
        slice_ = funding_df[(funding_df.index > from_ts) & (funding_df.index <= to_ts)]
        if slice_.empty:
            return 0.0
        total = 0.0
        for _, row in slice_.iterrows():
            rate = float(row["funding_rate"])
            # Longs pay when rate > 0, receive when < 0; shorts inverse.
            sign = -1.0 if position.side == Side.BUY else +1.0
            total += sign * rate * position.size_usd
        return total

    # ── Run ──────────────────────────────────────────────────────────────
    def run(self, start: datetime, end: datetime, symbol: str) -> tuple[list[Trade], pd.Series]:
        """
        Replay from start to end on a single symbol. Returns (trades,
        equity_curve indexed by timestamp).
        """
        df = self.klines.get(symbol)
        if df is None or df.empty:
            return [], pd.Series(dtype=float)
        funding_df = self.funding.get(symbol, pd.DataFrame())

        # Filter to [start, end]
        start = self._ensure_utc(start)
        end = self._ensure_utc(end)
        df = df[(df.index >= start) & (df.index <= end)].copy()
        if df.empty:
            return [], pd.Series(dtype=float)

        # Adapter setup
        eval_interval = self.adapter.evaluation_interval()
        lookback_bars = self.adapter.required_lookback_bars()

        trades: list[Trade] = []
        open_pos: Optional[_OpenPosition] = None
        capital = self.starting_capital
        equity_records: list[tuple[datetime, float]] = []

        last_eval_ts = df.index[0]
        for bar_ts in df.index:
            current_history = df[df.index <= bar_ts]
            if len(current_history) < lookback_bars:
                continue

            # ── Funding accrual on open position ──
            if open_pos is not None and not funding_df.empty:
                funding_pnl = self._accumulate_funding_pnl(
                    funding_df, open_pos,
                    pd.Timestamp(open_pos.last_funding_settle_ms, unit="ms", tz="UTC").to_pydatetime() if open_pos.last_funding_settle_ms else open_pos.entry_fill.timestamp,
                    bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts,
                )
                if funding_pnl != 0.0:
                    capital += funding_pnl
                    open_pos.last_funding_settle_ms = int(bar_ts.value // 1_000_000) if hasattr(bar_ts, "value") else int(bar_ts.timestamp() * 1000)

            # ── Throttled evaluation ──
            now_dt = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
            last_eval_dt = last_eval_ts.to_pydatetime() if hasattr(last_eval_ts, "to_pydatetime") else last_eval_ts
            since_last = now_dt - last_eval_dt
            if since_last >= eval_interval or bar_ts == df.index[0]:
                ctx = SnapshotContext(
                    now=now_dt,
                    symbol=symbol,
                    history=current_history,
                    funding_history=funding_df[funding_df.index <= bar_ts] if not funding_df.empty else pd.DataFrame(),
                    capital_usd=capital,
                    open_position_usd=open_pos.size_usd if open_pos else 0.0,
                    open_position_side=open_pos.side if open_pos else None,
                    adv_24h_usd=self._adv_24h_usd(df, now_dt),
                )
                signal = self.adapter.evaluate(ctx)
                last_eval_ts = bar_ts
                if signal is not None and signal.is_actionable():
                    open_pos, trade = self._process_signal(
                        signal=signal,
                        next_bar=self._next_bar(df, bar_ts),
                        symbol=symbol,
                        current_pos=open_pos,
                        adv_24h_usd=ctx.adv_24h_usd,
                    )
                    if trade is not None:
                        trades.append(trade)
                        capital += trade.realized_pnl_usd

            # Equity snapshot: capital + mark-to-market of open pos
            mtm = 0.0
            if open_pos is not None:
                close_price = float(df.loc[bar_ts, "close"])
                if open_pos.side == Side.BUY:
                    mtm = open_pos.size_usd * (close_price - open_pos.entry_fill.fill_price) / open_pos.entry_fill.fill_price
                else:
                    mtm = open_pos.size_usd * (open_pos.entry_fill.fill_price - close_price) / open_pos.entry_fill.fill_price
            equity_records.append((now_dt, capital + mtm))

        equity_curve = pd.Series(
            data=[e[1] for e in equity_records],
            index=pd.to_datetime([e[0] for e in equity_records], utc=True),
            name="equity",
        )
        return trades, equity_curve

    # ── Signal processing ────────────────────────────────────────────────
    def _process_signal(
        self,
        signal: Signal,
        next_bar: Optional[pd.Series],
        symbol: str,
        current_pos: Optional[_OpenPosition],
        adv_24h_usd: float,
    ) -> tuple[Optional[_OpenPosition], Optional[Trade]]:
        """
        Apply signal. Fills happen at the OPEN of the NEXT bar (no lookahead).
        Returns (new_open_position_or_None, completed_trade_or_None).
        """
        if next_bar is None:
            return current_pos, None

        # Build a snapshot at next bar's open
        snapshot = MarketSnapshot(
            timestamp=next_bar.name.to_pydatetime() if hasattr(next_bar.name, "to_pydatetime") else next_bar.name,
            symbol=symbol,
            mid_price=float(next_bar["open"]),
            high=float(next_bar["high"]),
            low=float(next_bar["low"]),
            adv_24h_usd=adv_24h_usd,
        )

        # ── EXIT ──
        if signal.action == SignalAction.EXIT:
            if current_pos is None:
                return None, None
            exit_side = Side.SELL if current_pos.side == Side.BUY else Side.BUY
            fill = self.exec_sim.execute_order(
                snapshot=snapshot,
                side=exit_side,
                size_usd=current_pos.size_usd,
                order_type=OrderType.TAKER,                       # exits are taker
            )
            if fill is None:
                return current_pos, None
            trade = Trade(
                symbol=symbol,
                side=current_pos.side,
                entry_fill=current_pos.entry_fill,
            )
            trade.close(fill, funding_pnl_usd=0.0)
            return None, trade

        # ── ENTER ── (skip if already in a position)
        if current_pos is not None:
            return current_pos, None
        side = Side.BUY if signal.action == SignalAction.ENTER_LONG else Side.SELL
        order_type = OrderType.MAKER if signal.metadata.get("maker", True) else OrderType.TAKER
        fill = self.exec_sim.execute_order(
            snapshot=snapshot,
            side=side,
            size_usd=signal.size_usd,
            order_type=order_type,
        )
        if fill is None:
            # Maker missed — try taker fallback if signal allows
            if signal.metadata.get("taker_fallback", False):
                fill = self.exec_sim.execute_order(
                    snapshot=snapshot, side=side, size_usd=signal.size_usd, order_type=OrderType.TAKER,
                )
            if fill is None:
                return current_pos, None
        new_pos = _OpenPosition(symbol=symbol, side=side, size_usd=signal.size_usd, entry_fill=fill)
        return new_pos, None

    @staticmethod
    def _next_bar(df: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
        idx = df.index.get_indexer([ts])[0]
        if idx < 0 or idx + 1 >= len(df):
            return None
        return df.iloc[idx + 1]

    @staticmethod
    def _ensure_utc(dt: datetime) -> Any:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return pd.Timestamp(dt).tz_convert("UTC")
