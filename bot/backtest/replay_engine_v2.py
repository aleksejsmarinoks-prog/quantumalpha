"""
QA Backtest — ReplayEngine v2 (Phase 6.3.1a Step 1)
======================================================

Replays historical bars through a strategy adapter. Closes the gap identified
in Phase 6.3.1 audit:

  Issues fixed vs v1:
    1. ✅ SCALE_IN support (tier 2/3 entries no longer lost) — main reason
         mean_reversion produced 0 trades in Phase 6.3 walk-forward
    2. ✅ Regime passed to adapter via SnapshotContext.regime
    3. ✅ BacktestRiskKernel hook (Step 4 will provide kernel; engine
         exposes the call point and respects vetoes)
    4. ⏳ Precomputed indicators — interface only; adapter or future
         IndicatorProvider populates SnapshotContext.indicators

Adapter contract:

    class MyAdapter:
        def evaluate(self, snapshot: SnapshotContext,
                     positions: list[Position]) -> list[Action]:
            ...

        # Optional callbacks:
        def on_fill(self, position: Position, fill: Fill) -> None: ...
        def on_position_closed(self, position: Position) -> None: ...

The engine respects time-direction: actions returned from `evaluate(bar_N)`
execute at bar_N+1 open (next-bar fill model). This is the standard
anti-lookahead pattern. Stop/take-profit fills happen INTRA-bar using
high/low (conservative — assumes adverse-direction first).

Author: QuantumAlpha
Phase: 6.3.1a Step 1
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Callable, Iterable, List, Optional, Protocol, Tuple

from .models import (
    Action, Bar, CloseAction, CloseReason, Fill, OpenAction, Position,
    ReduceAction, ScaleInAction, SnapshotContext, TakeProfit, Trade,
)

logger = logging.getLogger("qa.backtest.replay_v2")


# ---------------------------------------------------------------------------
# Adapter protocol — informational, not enforced (duck-typing)
# ---------------------------------------------------------------------------

class AdapterProtocol(Protocol):
    """Protocol that adapters should implement. evaluate() is required;
    callbacks are optional and probed via hasattr().
    """
    def evaluate(self, snapshot: SnapshotContext,
                 positions: List[Position]) -> List[Action]: ...


# ---------------------------------------------------------------------------
# Risk kernel hook — Step 4 will replace with real BacktestRiskKernel
# ---------------------------------------------------------------------------

class RiskKernelProtocol(Protocol):
    """Risk kernel called BEFORE every action dispatch. Returns True to
    allow, False to veto. Step 1 ships the hook only; Step 4 implements
    policy (per-position cap, daily loss limit, leverage limit, etc).
    """
    def allow_action(self, action: Action, positions: List[Position],
                     equity: float, snapshot: SnapshotContext) -> bool: ...


# ---------------------------------------------------------------------------
# BacktestResult
# ---------------------------------------------------------------------------

class BacktestResult:
    """Aggregated output of a backtest run. Exposed via summary() and
    raw access to .trades / .equity_curve / .open_positions.
    """
    def __init__(
        self,
        initial_equity: float,
        final_equity: float,
        trades: List[Trade],
        open_positions: List[Position],
        equity_curve: List[Tuple[datetime, float]],
        bars_processed: int,
        rejections: List[dict],
    ):
        self.initial_equity = initial_equity
        self.final_equity = final_equity
        self.trades = trades
        self.open_positions = open_positions
        self.equity_curve = equity_curve
        self.bars_processed = bars_processed
        self.rejections = rejections                 # actions vetoed by risk kernel

    @property
    def total_return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return ((self.final_equity - self.initial_equity) / self.initial_equity) * 100.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.realized_pnl > 0)
        return wins / len(self.trades)

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, equity in self.equity_curve:
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak * 100.0
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def summary(self) -> dict:
        return {
            "initial_equity": self.initial_equity,
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return_pct, 4),
            "trade_count": self.trade_count,
            "win_rate": round(self.win_rate, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "open_positions_at_end": len(self.open_positions),
            "bars_processed": self.bars_processed,
            "risk_kernel_rejections": len(self.rejections),
        }


# ---------------------------------------------------------------------------
# ReplayEngine v2
# ---------------------------------------------------------------------------

class ReplayEngineV2:
    """Multi-position event-driven replay engine.

    Order of operations per bar:
      1. Build SnapshotContext
      2. Check exits on open positions (stop_loss + take_profit, intra-bar)
      3. Call adapter.evaluate(snapshot, positions) → actions
      4. For each action: risk kernel check → dispatch
      5. Mark-to-market equity update
    """

    DEFAULT_SLIPPAGE_BPS = 5.0       # 0.05% one-way
    DEFAULT_COMMISSION_BPS = 7.5     # 0.075% per side (Bybit perp taker)

    def __init__(
        self,
        symbol: str,
        initial_equity: float = 1000.0,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        commission_bps: float = DEFAULT_COMMISSION_BPS,
        regime_provider: Optional[Callable[[datetime], Optional[str]]] = None,
        calendar_provider: Optional[Callable[[datetime], list]] = None,
        indicators_provider: Optional[Callable[[datetime, Bar], dict]] = None,
        risk_kernel: Optional[RiskKernelProtocol] = None,
    ):
        if initial_equity <= 0:
            raise ValueError("initial_equity must be > 0")
        if slippage_bps < 0 or commission_bps < 0:
            raise ValueError("slippage_bps and commission_bps must be >= 0")

        self.symbol = symbol
        self.initial_equity = initial_equity
        self.slippage_bps = slippage_bps
        self.commission_bps = commission_bps
        self.regime_provider = regime_provider
        self.calendar_provider = calendar_provider
        self.indicators_provider = indicators_provider
        self.risk_kernel = risk_kernel

        # Run state (reset on each .run() call)
        self._reset_state()

    # -----------------------------------------------------------------------
    # State management
    # -----------------------------------------------------------------------

    def _reset_state(self) -> None:
        self.equity: float = self.initial_equity
        self.positions: List[Position] = []
        self.trades: List[Trade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []
        self.bars_processed: int = 0
        self.rejections: List[dict] = []
        self._pending_actions: List[Action] = []        # actions for next bar (next-bar fill)
        self._exits_by_position: dict = {}              # accumulator for partial exits per bar

    # -----------------------------------------------------------------------
    # Main run loop
    # -----------------------------------------------------------------------

    def run(self, bars: Iterable[Bar], adapter: AdapterProtocol) -> BacktestResult:
        """Replay bars through adapter. Returns BacktestResult."""
        self._reset_state()

        bar_iter = iter(bars)
        previous_bar: Optional[Bar] = None

        for bar in bar_iter:
            if previous_bar is not None and bar.timestamp <= previous_bar.timestamp:
                raise ValueError(
                    f"Bars must be strictly increasing in time: "
                    f"{previous_bar.timestamp} >= {bar.timestamp}"
                )

            # 1. Execute pending actions from previous bar (next-bar fill model)
            if self._pending_actions:
                self._execute_pending_actions(bar, adapter)

            # 2. Check exits on open positions (intra-bar high/low traversal)
            self._check_exits_intrabar(bar, adapter)

            # 3. Build SnapshotContext
            snapshot = self._build_snapshot(bar)

            # 4. Ask adapter for new actions
            try:
                actions = adapter.evaluate(snapshot, list(self.positions))
            except Exception as e:
                logger.exception("Adapter evaluate() raised at %s: %s",
                                 bar.timestamp, e)
                actions = []

            if actions:
                # Risk-kernel filter NOW (vetoes are recorded immediately for telemetry)
                accepted: List[Action] = []
                for action in actions:
                    if self._risk_check(action, snapshot):
                        accepted.append(action)
                self._pending_actions = accepted

            # 5. Mark-to-market equity (uses bar.close as mark)
            self._update_equity_curve(bar)

            self.bars_processed += 1
            previous_bar = bar

        # Final: close any open positions at last bar close
        if self.positions and previous_bar is not None:
            for pos in list(self.positions):
                self._close_position(
                    pos,
                    exit_price=previous_bar.close,
                    timestamp=previous_bar.timestamp,
                    reason="end_of_data",
                    adapter=adapter,
                )

        return BacktestResult(
            initial_equity=self.initial_equity,
            final_equity=self.equity,
            trades=self.trades,
            open_positions=self.positions,
            equity_curve=self.equity_curve,
            bars_processed=self.bars_processed,
            rejections=self.rejections,
        )

    # -----------------------------------------------------------------------
    # Snapshot construction
    # -----------------------------------------------------------------------

    def _build_snapshot(self, bar: Bar) -> SnapshotContext:
        regime = None
        if self.regime_provider is not None:
            try:
                regime = self.regime_provider(bar.timestamp)
            except Exception as e:
                logger.warning("regime_provider raised: %s", e)

        macro_events: tuple = ()
        if self.calendar_provider is not None:
            try:
                macro_events = tuple(self.calendar_provider(bar.timestamp) or [])
            except Exception as e:
                logger.warning("calendar_provider raised: %s", e)

        indicators: dict = {}
        if self.indicators_provider is not None:
            try:
                indicators = self.indicators_provider(bar.timestamp, bar) or {}
            except Exception as e:
                logger.warning("indicators_provider raised: %s", e)

        return SnapshotContext(
            timestamp=bar.timestamp,
            symbol=self.symbol,
            bar=bar,
            spot=bar.close,
            equity=self.equity,
            open_position_count=len(self.positions),
            regime=regime,
            macro_events=macro_events,
            indicators=indicators,
        )

    # -----------------------------------------------------------------------
    # Risk kernel hook
    # -----------------------------------------------------------------------

    def _risk_check(self, action: Action, snapshot: SnapshotContext) -> bool:
        if self.risk_kernel is None:
            return True
        try:
            allowed = self.risk_kernel.allow_action(
                action, list(self.positions), self.equity, snapshot,
            )
        except Exception as e:
            logger.exception("Risk kernel raised — treating as VETO: %s", e)
            self.rejections.append({
                "timestamp": snapshot.timestamp.isoformat(),
                "action": action.__class__.__name__,
                "reason": f"kernel_error: {e}",
            })
            return False
        if not allowed:
            self.rejections.append({
                "timestamp": snapshot.timestamp.isoformat(),
                "action": action.__class__.__name__,
                "reason": "kernel_veto",
            })
        return allowed

    # -----------------------------------------------------------------------
    # Action dispatch (next-bar fill model)
    # -----------------------------------------------------------------------

    def _execute_pending_actions(self, bar: Bar, adapter: AdapterProtocol) -> None:
        """Execute actions queued from previous bar at this bar's open price."""
        actions = self._pending_actions
        self._pending_actions = []
        for action in actions:
            self._dispatch_action(action, bar, adapter)

    def _dispatch_action(self, action: Action, bar: Bar,
                         adapter: AdapterProtocol) -> None:
        if isinstance(action, OpenAction):
            self._open_position(action, bar, adapter)
        elif isinstance(action, ScaleInAction):
            self._scale_in(action, bar, adapter)
        elif isinstance(action, ReduceAction):
            self._reduce_position(action, bar, adapter)
        elif isinstance(action, CloseAction):
            self._close_action(action, bar, adapter)
        else:
            logger.warning("Unknown action type: %s", type(action).__name__)

    def _open_position(self, action: OpenAction, bar: Bar,
                       adapter: AdapterProtocol) -> None:
        if action.qty <= 0:
            logger.warning("OpenAction qty <= 0, ignored")
            return

        fill_price = self._apply_slippage(bar.open, action.side, is_entry=True)
        commission = self._commission(fill_price, action.qty)
        fill = Fill(
            timestamp=bar.timestamp,
            price=fill_price,
            qty=action.qty,
            commission=commission,
        )
        position = Position(
            id=str(uuid.uuid4())[:8],
            symbol=action.symbol,
            side=action.side,
            opened_at=bar.timestamp,
            stop_loss=action.stop_loss,
            take_profits=list(action.take_profits),
            metadata=dict(action.metadata),
        )
        position.add_fill(fill)
        self.equity -= commission   # commission paid immediately
        self.positions.append(position)

        logger.debug("OPEN %s %s qty=%.6f @ %.4f (slip+%.2fbps, comm=%.4f)",
                     action.side, action.symbol, action.qty,
                     fill_price, self.slippage_bps, commission)
        self._maybe_call(adapter, "on_fill", position, fill)

    def _scale_in(self, action: ScaleInAction, bar: Bar,
                  adapter: AdapterProtocol) -> None:
        position = self._find_position(action.position_id)
        if position is None or position.is_closed():
            logger.warning("ScaleInAction: position %s not found / closed",
                           action.position_id)
            return
        if action.qty <= 0:
            return

        fill_price = self._apply_slippage(bar.open, position.side, is_entry=True)
        commission = self._commission(fill_price, action.qty)
        fill = Fill(
            timestamp=bar.timestamp,
            price=fill_price,
            qty=action.qty,
            commission=commission,
        )
        position.add_fill(fill)
        self.equity -= commission

        logger.debug("SCALE_IN %s qty=%.6f @ %.4f (new avg=%.4f, total=%.6f)",
                     position.id, action.qty, fill_price,
                     position.avg_entry_price, position.size)
        self._maybe_call(adapter, "on_fill", position, fill)

    def _reduce_position(self, action: ReduceAction, bar: Bar,
                         adapter: AdapterProtocol) -> None:
        position = self._find_position(action.position_id)
        if position is None or position.is_closed():
            return
        qty = min(action.qty, position.size)
        if qty <= 0:
            return

        exit_price = self._apply_slippage(bar.open, position.side, is_entry=False)
        commission = self._commission(exit_price, qty)
        pnl = position.apply_reduction(qty, exit_price, commission)
        self.equity += pnl
        logger.debug("REDUCE %s qty=%.6f @ %.4f pnl=%.4f",
                     position.id, qty, exit_price, pnl)

        if position.is_closed():
            self._finalize_close(position, exit_price, bar.timestamp,
                                 "adapter", adapter)

    def _close_action(self, action: CloseAction, bar: Bar,
                      adapter: AdapterProtocol) -> None:
        position = self._find_position(action.position_id)
        if position is None or position.is_closed():
            return
        self._close_position(
            position,
            exit_price=bar.open,
            timestamp=bar.timestamp,
            reason="adapter",
            adapter=adapter,
        )

    # -----------------------------------------------------------------------
    # Intra-bar exit checks (stops + take-profits)
    # -----------------------------------------------------------------------

    def _check_exits_intrabar(self, bar: Bar, adapter: AdapterProtocol) -> None:
        """Walk open positions, check stop-loss and take-profit triggers
        against bar high/low. Conservative: assumes adverse-direction first
        (stop hits before tp on same bar for LONG: low first; for SHORT: high first).
        """
        for position in list(self.positions):
            if position.is_closed():
                continue

            # 1. Stop-loss
            if position.stop_loss is not None:
                if position.side == "LONG" and bar.low <= position.stop_loss:
                    self._close_position(position, position.stop_loss,
                                         bar.timestamp, "stop_loss", adapter)
                    continue
                if position.side == "SHORT" and bar.high >= position.stop_loss:
                    self._close_position(position, position.stop_loss,
                                         bar.timestamp, "stop_loss", adapter)
                    continue

            # 2. Take-profit ladder
            new_tps: List[TakeProfit] = []
            for tp in position.take_profits:
                if tp.triggered:
                    new_tps.append(tp)
                    continue
                trigger = (
                    (position.side == "LONG" and bar.high >= tp.price) or
                    (position.side == "SHORT" and bar.low <= tp.price)
                )
                if trigger:
                    qty_to_close = position.size * tp.fraction
                    if qty_to_close > 0:
                        commission = self._commission(tp.price, qty_to_close)
                        pnl = position.apply_reduction(qty_to_close, tp.price, commission)
                        self.equity += pnl
                        logger.debug("TP %s @%.4f frac=%.2f pnl=%.4f",
                                     position.id, tp.price, tp.fraction, pnl)
                    new_tps.append(replace(tp, triggered=True))
                    if position.is_closed():
                        # Finalize as TP-closed
                        position.take_profits = new_tps
                        self._finalize_close(position, tp.price, bar.timestamp,
                                             "take_profit", adapter)
                        break
                else:
                    new_tps.append(tp)
            else:
                # Loop completed without break (position still open)
                position.take_profits = new_tps

    # -----------------------------------------------------------------------
    # Position close finalization
    # -----------------------------------------------------------------------

    def _close_position(
        self, position: Position, exit_price: float,
        timestamp: datetime, reason: CloseReason, adapter: AdapterProtocol,
    ) -> None:
        if position.is_closed():
            return
        qty = position.size
        # If reason is stop_loss / take_profit / risk_kernel, exit_price is the
        # trigger price (already provided). Otherwise apply slippage on next-bar open.
        if reason == "adapter" or reason == "end_of_data":
            exit_price = self._apply_slippage(exit_price, position.side, is_entry=False)
        commission = self._commission(exit_price, qty)
        pnl = position.apply_reduction(qty, exit_price, commission)
        self.equity += pnl
        self._finalize_close(position, exit_price, timestamp, reason, adapter)

    def _finalize_close(
        self, position: Position, exit_price: float,
        timestamp: datetime, reason: CloseReason, adapter: AdapterProtocol,
    ) -> None:
        position.closed_at = timestamp
        position.close_reason = reason

        # Compute aggregated trade record
        qty_total = sum(f.qty for f in position.entries)
        trade = Trade(
            position_id=position.id,
            symbol=position.symbol,
            side=position.side,
            opened_at=position.opened_at,
            closed_at=timestamp,
            avg_entry_price=position.avg_entry_price,
            avg_exit_price=exit_price,
            qty_total=qty_total,
            realized_pnl=position.realized_pnl,
            commission=position.total_commission,
            close_reason=reason,
            metadata=dict(position.metadata),
        )
        self.trades.append(trade)

        # Remove from open positions
        try:
            self.positions.remove(position)
        except ValueError:
            pass

        logger.debug("CLOSED %s %s qty=%.6f entry=%.4f exit=%.4f pnl=%.4f reason=%s",
                     position.id, position.side, qty_total,
                     position.avg_entry_price, exit_price,
                     position.realized_pnl, reason)
        self._maybe_call(adapter, "on_position_closed", position)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _find_position(self, position_id: str) -> Optional[Position]:
        for p in self.positions:
            if p.id == position_id and not p.is_closed():
                return p
        return None

    def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
        """Slippage moves price against us:
          LONG entry  → price up
          LONG exit   → price down
          SHORT entry → price down
          SHORT exit  → price up
        """
        bps = self.slippage_bps / 10_000.0
        if is_entry:
            adj = bps if side == "LONG" else -bps
        else:
            adj = -bps if side == "LONG" else bps
        return price * (1.0 + adj)

    def _commission(self, price: float, qty: float) -> float:
        return abs(price * qty) * (self.commission_bps / 10_000.0)

    def _update_equity_curve(self, bar: Bar) -> None:
        # Mark-to-market includes unrealized PnL on open positions
        mtm = self.equity
        for p in self.positions:
            mtm += p.unrealized_pnl(bar.close)
        self.equity_curve.append((bar.timestamp, mtm))

    def _maybe_call(self, adapter: AdapterProtocol, name: str, *args) -> None:
        fn = getattr(adapter, name, None)
        if not callable(fn):
            return
        try:
            fn(*args)
        except Exception as e:
            logger.warning("Adapter %s() raised: %s", name, e)
