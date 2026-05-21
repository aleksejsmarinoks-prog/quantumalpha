"""
ProductionAdapter base class — Phase 6.3.1a Step 4 (rewrite for ReplayEngine v2).

Bridges PRODUCTION strategy classes (BaseStrategy subclasses) into the
ReplayEngine v2 adapter contract from Step 1.

Architecture
============

    Production BaseStrategy ──┐
      .evaluate()             ├──→ ProductionAdapter ──→ ReplayEngineV2
      .apply_risk_gates()     │      (this file)            (Step 1)
      .on_tier_filled()       │           │
      .on_position_closed()   │           │ owns
    ──────────────────────────┘           ▼
                                BacktestRiskKernel
                                (production-equiv)

Differences from legacy `bot/backtester/production_adapter_base.py`:
  - Implements Step 1 AdapterProtocol:
      evaluate(snapshot: SnapshotContext, positions: List[Position])
        -> List[Action]
      on_fill(position: Position, fill: Fill) -> None
      on_position_closed(position: Position) -> None
  - Uses Step 1 typed action dataclasses (OpenAction / ScaleInAction /
    ReduceAction / CloseAction) instead of legacy `Signal+SignalAction enum`.
  - Pulls regime from `snapshot.regime` (Step 1 SnapshotContext field,
    populated by Step 2 RegimeDetector via engine's regime_provider) —
    no separate regime_provider lookup in adapter.
  - Position-aware: snapshot exposes open positions, so SCALE_IN emits
    `ScaleInAction(position_id=...)` instead of legacy OPEN-with-metadata-flag.
  - Returns LIST of actions (engine supports multiple actions per bar)
    rather than at most one Signal.

Subclasses provide only three abstract methods:
  - prepare_market_data(snapshot) -> Optional[Dict]
  - get_stop_loss_pct(prod_signal, market_data) -> float
  - get_take_profit_pct(prod_signal, market_data) -> float

Subclass example (Step 5a will deliver mean_reversion adapter):

    class MeanReversionAdapter(ProductionAdapter):
        name = "mean_reversion"
        strategy_class = MeanReversionStrategy

        def prepare_market_data(self, snapshot):
            return {"last_price": snapshot.spot, "rsi_14_1h": snapshot.indicators.get("rsi", 50)}

        def get_stop_loss_pct(self, prod_signal, market_data):
            return 0.02

        def get_take_profit_pct(self, prod_signal, market_data):
            return 0.04

Author: QuantumAlpha
Phase: 6.3.1a Step 4
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .backtest_risk_kernel import (
    BacktestClock,
    BacktestRiskKernel,
    TradeApproval,
    TradeDecision,
    TradeRequest,
)
from .models import (
    Action,
    CloseAction,
    Fill,
    OpenAction,
    Position,
    ReduceAction,
    ScaleInAction,
    SnapshotContext,
)


logger = logging.getLogger("qa.backtest.production_adapter")


# ---------------------------------------------------------------------------
# Production signal-type sentinels
# ---------------------------------------------------------------------------
# We don't import bot.strategies.base_strategy.SignalType at module load
# because:
#   1. It creates a circular dep when ProductionAdapter is imported by
#      strategy code (some setups do this).
#   2. Tests need to run without the full production strategy stack.
#
# Subclasses import SignalType themselves and we duck-type on string values
# of the .signal_type.value (or `signal_type` attribute if string).
#
# Production SignalType values (verified from Phase 6.3.1):
#   "enter_long" / "enter_short" / "exit" / "hold" / "scale_in" / "reduce"
#
# An adapter may override _signal_type_value() if production naming changes.


# Production-signal sentinel constants (string values, NOT enum dependency)
PROD_ENTER_LONG  = "enter_long"
PROD_ENTER_SHORT = "enter_short"
PROD_EXIT        = "exit"
PROD_HOLD        = "hold"
PROD_SCALE_IN    = "scale_in"
PROD_REDUCE      = "reduce"


# ---------------------------------------------------------------------------
# ProductionAdapter base class
# ---------------------------------------------------------------------------

class ProductionAdapter(ABC):
    """Base class that wraps a production BaseStrategy for ReplayEngineV2.

    Implements the Step 1 AdapterProtocol:
      .evaluate(snapshot, positions) -> List[Action]
      .on_fill(position, fill)        -> None    (optional callback)
      .on_position_closed(position)   -> None    (optional callback)
    """

    # ── Subclass MUST set these ────────────────────────────────────────────
    name: str = "production_adapter_base"
    strategy_class: Optional[type] = None        # BaseStrategy subclass

    # ── Construction ───────────────────────────────────────────────────────

    def __init__(
        self,
        starting_capital_usd: float = 1000.0,
        risk_kernel: Optional[BacktestRiskKernel] = None,
        clock: Optional[BacktestClock] = None,
    ) -> None:
        if self.strategy_class is None:
            raise ValueError(
                f"{type(self).__name__} must set class attribute `strategy_class` "
                f"(BaseStrategy subclass)"
            )

        self.starting_capital_usd = starting_capital_usd
        # Shared clock between adapter and kernel — engine calls clock.set() per bar
        self.clock = clock or BacktestClock()

        # Build production strategy instance
        self.strategy = self._build_strategy()
        if hasattr(self.strategy, "set_strategy_capital"):
            self.strategy.set_strategy_capital(starting_capital_usd)

        # Risk kernel — production-equivalent. Adapter owns it (not engine).
        # Engine's `risk_kernel` hook stays as generic safety net for tests.
        self.risk_kernel = risk_kernel or BacktestRiskKernel(
            starting_equity_usd=starting_capital_usd,
            clock=self.clock,
        )

        # Telemetry
        self._signals_emitted: int = 0
        self._signals_gated: int = 0           # gated by production strategy itself
        self._signals_kernel_rejected: int = 0
        self._signals_kernel_halted: int = 0
        self._signals_kernel_reduced: int = 0
        self._signals_executed: int = 0

    # ─── ReplayEngine v2 AdapterProtocol contract ──────────────────────────

    def evaluate(
        self,
        snapshot: SnapshotContext,
        positions: List[Position],
    ) -> List[Action]:
        """Called by ReplayEngineV2 each bar. Returns list of actions to dispatch.

        Workflow:
          1. Sync clock to snapshot.timestamp
          2. prepare_market_data(snapshot) — subclass-specific
          3. Get regime (from snapshot.regime — already populated by engine
             via Step 2 regime_provider)
          4. Production strategy.evaluate(symbol, market_data, regime)
          5. apply_risk_gates(prod_signal, regime, now=snapshot.timestamp)
          6. If actionable: build TradeRequest -> kernel.approve_trade()
          7. Convert approved production Signal -> Step 1 Action(s)
          8. Return List[Action] (often single-element, but engine supports multi)
        """
        # 1. Sync shared clock — kernel reads this for time-based killswitches
        self.clock.set(snapshot.timestamp)
        self.risk_kernel.maybe_reset_periods()

        # 2. Build market_data (subclass)
        try:
            market_data = self.prepare_market_data(snapshot)
        except Exception as e:
            logger.warning(
                "prepare_market_data failed at %s for %s: %s",
                snapshot.timestamp, snapshot.symbol, e,
            )
            return []
        if market_data is None:
            return []   # warmup / insufficient data

        # 3. Regime — already populated by engine
        regime = snapshot.regime or "NEUTRAL"

        # 3b. Phase 6.3.1b Q6.3 fix — sync strategy capital with current kernel equity
        # per tick. Production strategies require this for tier sizing computation;
        # otherwise tier1_usd may be 0 → all signals rejected at size_below_min →
        # reproduces Phase 6.3 "0 trades" false negative.
        if hasattr(self.strategy, "set_strategy_capital"):
            capital_pct = getattr(self.strategy, "capital_pct", 1.0)
            try:
                current_strategy_capital = self.risk_kernel.current_equity * capital_pct
                self.strategy.set_strategy_capital(current_strategy_capital)
            except Exception as e:
                logger.debug("set_strategy_capital sync failed at %s: %s",
                             snapshot.timestamp, e)

        # 4. Production evaluate (with time injection — Step 3 patches + 6.3.1b-B Bug-1 fix)
        # CRITICAL: `now=snapshot.timestamp` injection prevents wall-clock fallback in
        # production strategy's _evaluate_existing_position() time-based logic
        # (age_hours / MAX_HOLD_HOURS / time_stop). Without this, backtest timestamps
        # differ from wall-clock by months → every position exited on time_stop
        # immediately after open → tier 2/3 scale-ins never reached.
        try:
            prod_signal = self._call_with_optional_now(
                self.strategy.evaluate,
                snapshot.symbol, market_data, regime,
                now=snapshot.timestamp,
            )
        except Exception as e:
            logger.warning("strategy.evaluate raised at %s: %s",
                           snapshot.timestamp, e)
            return []

        self._signals_emitted += 1

        # 5. Production risk gates (with time injection — Step 3 patches)
        if hasattr(self.strategy, "apply_risk_gates"):
            prod_signal = self._call_with_optional_now(
                self.strategy.apply_risk_gates,
                prod_signal, regime,
                now=snapshot.timestamp,
            )

        if not self._is_prod_signal_actionable(prod_signal):
            self._signals_gated += 1
            return []

        # Kernel approval only needed for ENTRIES (capital allocation).
        # EXIT / REDUCE / HOLD are de-risking — no approval required.
        st_val = self._signal_type_value(prod_signal)
        is_entry = st_val in (PROD_ENTER_LONG, PROD_ENTER_SHORT, PROD_SCALE_IN)

        if is_entry:
            # 6. Kernel approval (entries only)
            try:
                trade_req = self._build_trade_request(prod_signal, regime, snapshot, positions, market_data)
            except Exception as e:
                logger.warning("_build_trade_request raised: %s", e)
                return []

            approval = self.risk_kernel.approve_trade(trade_req)

            if approval.decision == TradeDecision.HALTED:
                self._signals_kernel_halted += 1
                return []
            if approval.decision == TradeDecision.REJECTED:
                self._signals_kernel_rejected += 1
                return []
            if approval.decision == TradeDecision.REDUCED_SIZE:
                self._signals_kernel_reduced += 1
            approved_size_usd = approval.approved_size_usd
        else:
            # De-risking — no kernel call, use the signal's own size
            approved_size_usd = float(getattr(prod_signal, "size_usd", 0.0))

        # 7. Convert production signal → Step 1 action(s)
        actions = self._convert_signal_to_actions(
            prod_signal,
            approved_size_usd=approved_size_usd,
            snapshot=snapshot,
            positions=positions,
            market_data=market_data,
        )

        if actions:
            self._signals_executed += 1
        return actions

    def on_fill(self, position: Position, fill: Fill) -> None:
        """ReplayEngine v2 callback after a fill. Drives strategy state.

        Position has been opened or scaled in. Notify production strategy
        if it has the corresponding hooks.
        """
        # Determine tier from number of fills accumulated on this position
        tier = len(position.entries)
        symbol = position.symbol
        fill_price = fill.price
        fill_size_usd = fill.price * fill.qty
        timestamp = fill.timestamp

        if hasattr(self.strategy, "on_tier_filled"):
            try:
                self._call_with_optional_now(
                    self.strategy.on_tier_filled,
                    symbol, tier, fill_price, fill_size_usd,
                    now=timestamp,
                )
            except Exception as e:
                logger.warning("strategy.on_tier_filled raised: %s", e)
        elif hasattr(self.strategy, "on_position_opened") and tier == 1:
            try:
                self._call_with_optional_now(
                    self.strategy.on_position_opened,
                    symbol, position.side.lower(), fill_size_usd, fill_price,
                    now=timestamp,
                )
            except Exception as e:
                logger.warning("strategy.on_position_opened raised: %s", e)

    def on_position_closed(self, position: Position) -> None:
        """ReplayEngine v2 callback after a position closes.

        Drives strategy state + records trade outcome with risk kernel
        (so kernel updates DD / consecutive losses / killswitches).
        """
        pnl_usd = position.realized_pnl
        was_loss = pnl_usd < 0
        timestamp = position.closed_at or self.clock.now()

        if hasattr(self.strategy, "on_position_closed"):
            try:
                self._call_with_optional_now(
                    self.strategy.on_position_closed,
                    position.symbol, pnl_usd, was_loss,
                    now=timestamp,
                )
            except Exception as e:
                logger.warning("strategy.on_position_closed raised: %s", e)

        # Always notify kernel — drives DD, killswitches
        try:
            self.risk_kernel.record_trade_outcome(pnl_usd, position.symbol)
        except Exception as e:
            logger.warning("kernel.record_trade_outcome raised: %s", e)

    # ─── Subclass-required (abstract) ──────────────────────────────────────

    @abstractmethod
    def prepare_market_data(
        self, snapshot: SnapshotContext,
    ) -> Optional[Dict[str, Any]]:
        """Convert SnapshotContext → strategy market_data dict.

        Strategy-specific. Examples:
          - mean_reversion: needs returns_1h, rsi_14_1h, last_price (resample 5m→1h)
          - funding_arb:    needs spot+perp prices, funding history
          - dca_dips:       needs active macro events, drawdown from session_open

        Returns None when insufficient warmup data — adapter skips this bar.
        """
        ...

    @abstractmethod
    def get_stop_loss_pct(
        self, prod_signal: Any, market_data: Dict[str, Any],
    ) -> float:
        """Stop-loss pct for TradeRequest. Strategy-specific.

        Used for kernel position sizing (risk_based_size = risk_dollars / stop_loss_pct).
        Should return the INITIAL stop, not an absolute drawdown cap.
        Return as positive fraction (e.g. 0.02 for 2%).
        """
        ...

    @abstractmethod
    def get_take_profit_pct(
        self, prod_signal: Any, market_data: Dict[str, Any],
    ) -> float:
        """Take-profit pct for TradeRequest. Strategy-specific.

        Used for R:R sanity warning. Return as positive fraction.
        """
        ...

    # ─── Optional subclass hooks ───────────────────────────────────────────

    def _build_strategy(self, **params) -> Any:
        """Default construction. Override if strategy needs special params."""
        if self.strategy_class is None:
            raise ValueError("strategy_class is None")
        # Try the standard 2-param signature first; fall back to default ctor
        try:
            return self.strategy_class(capital_pct=1.0, enabled=True, **params)
        except TypeError:
            try:
                return self.strategy_class(**params)
            except TypeError:
                return self.strategy_class()

    def required_lookback_bars(self) -> int:
        """Override if strategy needs > 50 warmup bars."""
        return 50

    def evaluation_interval(self) -> timedelta:
        """Override for strategies that don't evaluate every bar."""
        return timedelta(minutes=5)

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _signal_type_value(self, prod_signal: Any) -> str:
        """Extract the string value of prod_signal.signal_type.

        Handles both Enum and plain-string signal_type attributes.
        Override if production naming differs.
        """
        st = getattr(prod_signal, "signal_type", None)
        if st is None:
            return ""
        # If it's an Enum with .value
        val = getattr(st, "value", st)
        return str(val).lower()

    def _is_prod_signal_actionable(self, prod_signal: Any) -> bool:
        """Mirror production Signal.is_actionable() with safe duck-typing."""
        if not prod_signal:
            return False
        if hasattr(prod_signal, "is_actionable"):
            try:
                return bool(prod_signal.is_actionable())
            except Exception:
                pass
        # Fallback: actionable iff signal_type is not HOLD
        return self._signal_type_value(prod_signal) != PROD_HOLD

    def _build_trade_request(
        self,
        prod_signal: Any,
        regime: str,
        snapshot: SnapshotContext,
        positions: List[Position],
        market_data: Dict[str, Any],
    ) -> TradeRequest:
        """Build TradeRequest from production Signal for kernel approval."""
        st_val = self._signal_type_value(prod_signal)
        if st_val in (PROD_ENTER_LONG, PROD_SCALE_IN):
            side = "long"
        elif st_val == PROD_ENTER_SHORT:
            side = "short"
        else:
            side = "long"   # EXIT/REDUCE — side is informational only

        s13_active = regime in ("STAGFLATION_WAR", "S13", "STAGFLATION")
        vix_level = float(market_data.get("vix_level", 20.0))

        # Compute current leverage from open positions (sum notional / equity)
        if positions and snapshot.equity > 0:
            total_notional = sum(
                p.size * p.avg_entry_price for p in positions if not p.is_closed()
            )
            current_leverage = total_notional / snapshot.equity
        else:
            current_leverage = 0.0

        # Strategy name — prefer get_strategy_id, fall back to adapter.name
        if hasattr(self.strategy, "get_strategy_id"):
            try:
                strategy_name = self.strategy.get_strategy_id()
            except Exception:
                strategy_name = self.name
        else:
            strategy_name = self.name

        return TradeRequest(
            asset=snapshot.symbol,
            side=side,
            proposed_size_usd=float(getattr(prod_signal, "size_usd", 0.0)),
            stop_loss_pct=abs(self.get_stop_loss_pct(prod_signal, market_data)),
            take_profit_pct=abs(self.get_take_profit_pct(prod_signal, market_data)),
            strategy_name=strategy_name,
            confidence=float(getattr(prod_signal, "confidence", 0.5)),
            market_regime=regime,
            s13_active=s13_active,
            vix_level=vix_level,
            current_leverage=current_leverage,
        )

    def _convert_signal_to_actions(
        self,
        prod_signal: Any,
        approved_size_usd: float,
        snapshot: SnapshotContext,
        positions: List[Position],
        market_data: Dict[str, Any],
    ) -> List[Action]:
        """Convert production Signal → list of Step 1 Action dataclasses.

        Translation table:
          ENTER_LONG / ENTER_SHORT → OpenAction
          SCALE_IN (with existing position) → ScaleInAction
          SCALE_IN (no existing position) → OpenAction (defensive)
          REDUCE → ReduceAction
          EXIT → CloseAction
          HOLD → []
        """
        st_val = self._signal_type_value(prod_signal)

        # Compute qty from approved_size_usd and current spot
        # (If subclass needs different sizing, override this method.)
        spot = snapshot.spot
        if spot <= 0:
            logger.warning("snapshot.spot <= 0, can't compute qty; skipping")
            return []
        qty = approved_size_usd / spot

        # Metadata preserved across translation
        meta_in = getattr(prod_signal, "metadata", None)
        metadata: Dict[str, Any] = dict(meta_in) if isinstance(meta_in, dict) else {}
        metadata.update({
            "strategy_id": getattr(prod_signal, "strategy_id", self.name),
            "confidence": getattr(prod_signal, "confidence", None),
            "reason": getattr(prod_signal, "reason", None),
            "production_signal_type": st_val,
            "approved_size_usd": approved_size_usd,
        })

        # Find existing open position for this symbol (if any)
        existing = next(
            (p for p in positions
             if p.symbol == snapshot.symbol and not p.is_closed()),
            None,
        )

        if st_val == PROD_ENTER_LONG:
            return [OpenAction(
                symbol=snapshot.symbol,
                side="LONG",
                qty=qty,
                metadata=metadata,
            )]
        if st_val == PROD_ENTER_SHORT:
            return [OpenAction(
                symbol=snapshot.symbol,
                side="SHORT",
                qty=qty,
                metadata=metadata,
            )]
        if st_val == PROD_SCALE_IN:
            if existing is not None:
                return [ScaleInAction(
                    position_id=existing.id,
                    qty=qty,
                    metadata=metadata,
                )]
            # Defensive: SCALE_IN with no existing position → treat as OPEN
            logger.warning(
                "SCALE_IN received but no existing position for %s — opening new",
                snapshot.symbol,
            )
            return [OpenAction(
                symbol=snapshot.symbol,
                side="LONG",   # SCALE_IN historically long-only
                qty=qty,
                metadata=metadata,
            )]
        if st_val == PROD_REDUCE:
            if existing is not None:
                return [ReduceAction(
                    position_id=existing.id,
                    qty=min(qty, existing.size),
                )]
            return []
        if st_val == PROD_EXIT:
            if existing is not None:
                return [CloseAction(
                    position_id=existing.id,
                    reason="adapter",
                )]
            return []
        # HOLD or unknown
        return []

    @staticmethod
    def _call_with_optional_now(func, *args, now: Optional[datetime] = None, **kwargs):
        """Call func with `now=` kwarg if signature accepts it, else without.

        Step 3 patches added optional `now=` to production strategy methods.
        This helper makes adapter compatible with both patched and unpatched
        strategy code.
        """
        try:
            sig = inspect.signature(func)
            if "now" in sig.parameters:
                return func(*args, now=now, **kwargs)
        except (TypeError, ValueError):
            pass
        return func(*args, **kwargs)

    # ─── Walk-forward support ──────────────────────────────────────────────

    def reset(self, params: Optional[dict] = None) -> None:
        """Reset adapter + kernel for a new walk-forward window.

        Rebuilds the strategy (so internal state is fresh), resets the
        kernel to starting equity, zeros telemetry counters.
        """
        params = params or {}
        self.strategy = self._build_strategy(**params)
        if hasattr(self.strategy, "set_strategy_capital"):
            self.strategy.set_strategy_capital(self.starting_capital_usd)
        self.risk_kernel.reset(starting_equity_usd=self.starting_capital_usd)
        self._signals_emitted = 0
        self._signals_gated = 0
        self._signals_kernel_rejected = 0
        self._signals_kernel_halted = 0
        self._signals_kernel_reduced = 0
        self._signals_executed = 0

    # ─── Telemetry ─────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "signals_emitted": self._signals_emitted,
            "signals_gated_by_strategy": self._signals_gated,
            "signals_kernel_halted": self._signals_kernel_halted,
            "signals_kernel_rejected": self._signals_kernel_rejected,
            "signals_kernel_reduced": self._signals_kernel_reduced,
            "signals_executed": self._signals_executed,
            "risk_kernel_status": self.risk_kernel.get_status(),
        }
