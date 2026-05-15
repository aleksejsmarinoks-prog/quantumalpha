"""
ProductionAdapter base class for QA backtester.

Phase 6.3.1 — Production Bridge.
Build: 2026-05-14

Bridges PRODUCTION strategy classes (BaseStrategy subclasses) into
the backtester's adapter interface (StrategyAdapterProto).

Architecture:
    Production strategy ──┐
        + apply_risk_gates ├──→ ProductionAdapter ──→ Backtester ReplayEngine
        + on_tier_filled   │
        + on_position_closed
    ──────────────────────┘

Responsibilities of this base class:
  1. Wrap a BaseStrategy instance (mean_reversion / funding_arb / dca_dips)
  2. Transform SnapshotContext → strategy market_data dict
     (PER-STRATEGY OVERRIDE in subclasses — `prepare_market_data()`)
  3. Pass regime explicitly to strategy.evaluate()
  4. Call strategy.apply_risk_gates(signal, regime, now=ctx.now)
  5. Drive BacktestRiskKernel.approve_trade() per signal
  6. Convert production Signal → backtester Signal (handle SCALE_IN, REDUCE)
  7. Drive state callbacks (on_tier_filled, on_position_closed) on fills

Subclasses provide ONLY:
  - prepare_market_data(ctx) — strategy-specific market_data dict
  - get_stop_loss_pct() — for TradeRequest building (strategy-specific)
  - Optional: override signal conversion for unusual mappings

DEPENDENCIES (Phase 6.3.1 Step 0 — time injection):
  - BaseStrategy.apply_risk_gates() must accept `now: Optional[datetime] = None`
  - mean_reversion._evaluate_existing_position must accept `now=...`
  - mean_reversion.on_tier_filled must accept `now=...`
  This is a SOFT dependency — adapter will fall back to wall clock if param
  not accepted, but cooldowns and age_hours will be incorrect.

Author: Claude (Project advisor, QA Phase 6.3.1)
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional

from .backtest_risk_kernel import (
    BacktestClock,
    BacktestRiskKernel,
    TradeApproval,
    TradeDecision,
    TradeRequest,
)


log = logging.getLogger("qa.backtester.production_adapter")


class ProductionAdapter(ABC):
    """
    Base adapter that wraps a production BaseStrategy for backtest use.

    Implements StrategyAdapterProto from replay_engine.py:
      - name: str
      - reset(params: dict) -> None
      - evaluate(ctx: SnapshotContext) -> Optional[Signal (backtester)]
      - required_lookback_bars() -> int
      - evaluation_interval() -> timedelta
    """

    # Subclass MUST set these
    name: str = "production_adapter_base"
    strategy_class: Optional[type] = None    # BaseStrategy subclass

    def __init__(
        self,
        starting_capital_usd: float = 1000.0,
        clock: Optional[BacktestClock] = None,
        regime_provider: Optional[object] = None,
    ) -> None:
        """
        Args:
            starting_capital_usd: Strategy capital allocation for this run
            clock: BacktestClock instance — shared across adapter + risk_kernel
            regime_provider: object with .get_regime(timestamp, symbol) -> str
                             None → defaults to "NEUTRAL" (production fallback)
        """
        if self.strategy_class is None:
            raise ValueError(
                f"{type(self).__name__} must set class attribute `strategy_class`"
            )

        self.starting_capital_usd = starting_capital_usd
        self.clock = clock or BacktestClock()
        self.regime_provider = regime_provider

        # Instantiate production strategy
        self.strategy = self._build_strategy()

        # Mirror strategy capital allocation
        if hasattr(self.strategy, "set_strategy_capital"):
            self.strategy.set_strategy_capital(starting_capital_usd)

        # Risk kernel (production-equivalent)
        self.risk_kernel = BacktestRiskKernel(
            starting_equity_usd=starting_capital_usd,
            clock=self.clock,
        )

        # Stats
        self._signals_emitted = 0
        self._signals_gated = 0
        self._signals_kernel_rejected = 0
        self._signals_executed = 0

    # ─── StrategyAdapterProto contract ─────────────────────────────────────

    def reset(self, params: dict) -> None:
        """Called by walk-forward before each window/optimisation run."""
        # Rebuild strategy with new params (subclass may override _build_strategy)
        self.strategy = self._build_strategy(**params)
        if hasattr(self.strategy, "set_strategy_capital"):
            self.strategy.set_strategy_capital(self.starting_capital_usd)
        self.risk_kernel.reset(starting_equity_usd=self.starting_capital_usd)
        self._signals_emitted = 0
        self._signals_gated = 0
        self._signals_kernel_rejected = 0
        self._signals_executed = 0

    def evaluate(self, ctx) -> Optional[Any]:
        """
        Main entry point called by ReplayEngine each bar.

        Workflow:
          1. Sync clock to bar timestamp
          2. Build market_data dict from ctx (subclass-specific)
          3. Get regime
          4. strategy.evaluate(symbol, market_data, regime)
          5. strategy.apply_risk_gates(signal, regime, now=ctx.now)
          6. If actionable: build TradeRequest, get TradeApproval
          7. Convert production Signal → backtester Signal with approved size
          8. Return Optional[backtester Signal]
        """
        # 1. Sync clock
        self.clock.set(ctx.now)
        self.risk_kernel.maybe_reset_periods()

        # 2. Build market_data (subclass-specific)
        try:
            market_data = self.prepare_market_data(ctx)
        except Exception as e:
            log.warning(f"prepare_market_data failed for {ctx.symbol} at {ctx.now}: {e}")
            return None

        if market_data is None:
            return None  # insufficient data / warmup

        # 3. Regime
        regime = self._get_regime(ctx.now, ctx.symbol)

        # 4. Production evaluate
        prod_signal = self.strategy.evaluate(ctx.symbol, market_data, regime)
        self._signals_emitted += 1

        # 5. Production risk gates (with time injection)
        prod_signal = self._call_with_optional_now(
            self.strategy.apply_risk_gates,
            prod_signal, regime,
            now=ctx.now,
        )

        # Check actionability per production Signal
        if not self._is_prod_signal_actionable(prod_signal):
            self._signals_gated += 1
            return None

        # 6. Build TradeRequest for risk kernel
        trade_req = self._build_trade_request(prod_signal, regime, ctx)
        approval = self.risk_kernel.approve_trade(trade_req)

        if approval.decision == TradeDecision.HALTED:
            self._signals_kernel_rejected += 1
            return None
        if approval.decision == TradeDecision.REJECTED:
            self._signals_kernel_rejected += 1
            return None

        # 7. Convert to backtester Signal with approved size
        bt_signal = self._convert_signal(
            prod_signal,
            approved_size_usd=approval.approved_size_usd,
            ctx=ctx,
        )
        self._signals_executed += 1
        return bt_signal

    def required_lookback_bars(self) -> int:
        """Override in subclass if strategy needs > 50 bars."""
        return 50

    def evaluation_interval(self):
        """Override in subclass. Default: every bar."""
        from datetime import timedelta
        return timedelta(minutes=5)

    # ─── Hooks for ReplayEngine (called after fill / close) ───────────────

    def on_fill(
        self,
        symbol: str,
        tier: int,
        fill_price: float,
        fill_size_usd: float,
        timestamp: datetime,
    ) -> None:
        """Called by ReplayEngine after a simulated fill. Drives strategy state."""
        if hasattr(self.strategy, "on_tier_filled"):
            self._call_with_optional_now(
                self.strategy.on_tier_filled,
                symbol, tier, fill_price, fill_size_usd,
                now=timestamp,
            )
        elif hasattr(self.strategy, "on_position_opened"):
            self._call_with_optional_now(
                self.strategy.on_position_opened,
                symbol, "long", fill_size_usd, fill_price,
                now=timestamp,
            )

    def on_position_closed(
        self,
        symbol: str,
        pnl_usd: float,
        was_loss: bool,
        timestamp: datetime,
    ) -> None:
        """Called by ReplayEngine after a position closes. Drives strategy state."""
        if hasattr(self.strategy, "on_position_closed"):
            self._call_with_optional_now(
                self.strategy.on_position_closed,
                symbol, pnl_usd, was_loss,
                now=timestamp,
            )
        # Also notify risk kernel
        self.risk_kernel.record_trade_outcome(pnl_usd, symbol)

    # ─── Subclass-required methods ─────────────────────────────────────────

    @abstractmethod
    def prepare_market_data(self, ctx) -> Optional[Dict[str, Any]]:
        """Convert SnapshotContext → strategy market_data dict.

        Strategy-specific. Examples:
          - mean_reversion: needs returns_1h, rsi_14_1h, last_price (resample 5m→1h)
          - funding_arb: needs spot+perp prices, funding history
          - dca_dips: needs active macro events, drawdown from session_open

        Returns None if insufficient data (warmup).
        """
        ...

    @abstractmethod
    def get_stop_loss_pct(self, prod_signal, market_data: Dict) -> float:
        """Return stop-loss pct for TradeRequest construction.

        Strategy-specific. E.g. mean_reversion uses ABSOLUTE_STOP_PCT = -0.15
        but stop-loss for RiskKernel must be the *initial* stop, not the
        absolute one.
        """
        ...

    @abstractmethod
    def get_take_profit_pct(self, prod_signal, market_data: Dict) -> float:
        """Return take-profit pct for TradeRequest construction."""
        ...

    # ─── Hooks (optional override) ─────────────────────────────────────────

    def _build_strategy(self, **params) -> Any:
        """Default strategy construction. Override if non-default params needed."""
        return self.strategy_class(capital_pct=1.0, enabled=True, **params)

    # ─── Internal helpers ──────────────────────────────────────────────────

    def _get_regime(self, timestamp: datetime, symbol: str) -> str:
        """Get regime at this bar. Uses provider if available, else NEUTRAL."""
        if self.regime_provider is not None:
            try:
                return self.regime_provider.get_regime(timestamp, symbol)
            except Exception as e:
                log.warning(f"regime_provider failed: {e}")
        return "NEUTRAL"

    def _is_prod_signal_actionable(self, prod_signal) -> bool:
        """Check production Signal.is_actionable() — may be SCALE_IN / REDUCE."""
        if not prod_signal:
            return False
        if hasattr(prod_signal, "is_actionable"):
            return prod_signal.is_actionable()
        return False

    def _build_trade_request(self, prod_signal, regime: str, ctx) -> TradeRequest:
        """Build TradeRequest from production Signal for risk kernel approval."""
        market_data = self.prepare_market_data(ctx) or {}

        # Side mapping
        from bot.strategies.base_strategy import SignalType
        if prod_signal.signal_type in (SignalType.ENTER_LONG, SignalType.SCALE_IN):
            side = "long"
        elif prod_signal.signal_type == SignalType.ENTER_SHORT:
            side = "short"
        else:
            side = "long"  # default for EXIT / REDUCE

        # Stress flags (regime → s13_active, derive vix from regime/extras)
        s13_active = regime in ("STAGFLATION_WAR", "S13", "STAGFLATION")
        vix_level = market_data.get("vix_level", 20.0)  # subclass may inject

        return TradeRequest(
            asset=ctx.symbol,
            side=side,
            proposed_size_usd=prod_signal.size_usd,
            stop_loss_pct=abs(self.get_stop_loss_pct(prod_signal, market_data)),
            take_profit_pct=abs(self.get_take_profit_pct(prod_signal, market_data)),
            strategy_name=self.strategy.get_strategy_id() if hasattr(self.strategy, "get_strategy_id") else self.name,
            confidence=prod_signal.confidence,
            market_regime=regime,
            s13_active=s13_active,
            vix_level=vix_level,
            current_leverage=ctx.open_position_usd / ctx.capital_usd if ctx.capital_usd > 0 else 0.0,
        )

    def _convert_signal(self, prod_signal, approved_size_usd: float, ctx) -> Any:
        """Convert production Signal → backtester Signal.

        backtester `SignalAction` has 4 values; production `SignalType` has 6.
        SCALE_IN → ENTER_LONG with metadata flag
        REDUCE → EXIT (partial)  — full handling requires ReplayEngine v2

        Returns: bot.backtester.models.Signal (backtester type)
        """
        # Import here to avoid circular deps
        from .models import Signal, SignalAction
        from bot.strategies.base_strategy import SignalType

        action_map = {
            SignalType.ENTER_LONG:  SignalAction.ENTER_LONG,
            SignalType.ENTER_SHORT: SignalAction.ENTER_SHORT,
            SignalType.EXIT:        SignalAction.EXIT,
            SignalType.HOLD:        SignalAction.HOLD,
        }

        # SCALE_IN handling — depends on ReplayEngine v2
        if prod_signal.signal_type == SignalType.SCALE_IN:
            action = SignalAction.ENTER_LONG  # for now; ReplayEngine v2 needs SCALE_IN
            scale_in_meta = True
        elif prod_signal.signal_type == SignalType.REDUCE:
            action = SignalAction.EXIT
            scale_in_meta = False
            log.warning(f"REDUCE signal converted to full EXIT — ReplayEngine v2 needed for partial")
        else:
            action = action_map.get(prod_signal.signal_type, SignalAction.HOLD)
            scale_in_meta = False

        # Build metadata dict — preserve production info
        metadata = dict(prod_signal.metadata) if isinstance(prod_signal.metadata, dict) else {}
        metadata.update({
            "strategy_id": prod_signal.strategy_id,
            "confidence": prod_signal.confidence,
            "reason": prod_signal.reason,
            "production_signal_type": prod_signal.signal_type.value,
            "is_scale_in": scale_in_meta,
            "approved_size_usd": approved_size_usd,
        })

        return Signal(
            timestamp=ctx.now,
            symbol=ctx.symbol,
            action=action,
            size_usd=approved_size_usd,
            metadata=metadata,
        )

    @staticmethod
    def _call_with_optional_now(func, *args, now: Optional[datetime] = None, **kwargs):
        """
        Call function with `now=` kwarg if it accepts it, else without.
        This handles Step 0 (time injection) gracefully — works whether or not
        strategy code has been updated to accept `now` parameter.
        """
        try:
            sig = inspect.signature(func)
            if "now" in sig.parameters:
                return func(*args, now=now, **kwargs)
        except (TypeError, ValueError):
            pass
        return func(*args, **kwargs)

    # ─── Inspection ────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "signals_emitted": self._signals_emitted,
            "signals_gated_by_strategy": self._signals_gated,
            "signals_rejected_by_kernel": self._signals_kernel_rejected,
            "signals_executed": self._signals_executed,
            "risk_kernel_status": self.risk_kernel.get_status(),
        }
