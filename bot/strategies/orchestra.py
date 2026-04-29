"""
QuantumAlpha — Strategy Orchestra
==================================

Multi-strategy coordinator. Decides which strategies are active, how much
capital each gets, and which signals to execute when multiple strategies
fire on the same symbol.

Design principles:
    1. Capital is finite and explicitly allocated per strategy
    2. Signals from all strategies are collected, then RANKED, then executed
    3. Conflicts on same symbol → highest-confidence wins; opposite-direction
       conflicts (one says LONG, another SHORT) → BOTH cancelled (paranoid safety)
    4. Risk Kernel applies final veto across ALL signals before any execution
    5. Regime-based strategy enable/disable (e.g. mean-reversion off in PARABOLIC)

Reference architecture:
    - Pythagoras hedge fund: multi-strategy market-neutral with explicit
      capital weights, never overfit to single regime
    - Bridgewater All Weather: regime-conditional strategy weights

Version: 1.0 (commit #004)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bot.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalType,
    StrategyStatus,
)


logger = logging.getLogger("qa.orchestra")


# Regime → Strategy enablement matrix.
# Strategies not listed for a regime are DISABLED for that regime.
REGIME_STRATEGY_MATRIX: Dict[str, List[str]] = {
    "BULLISH":            ["funding_arb_v1", "mean_reversion_v1"],
    "NEUTRAL":            ["funding_arb_v1", "mean_reversion_v1"],
    "VOLATILE":           ["funding_arb_v1", "mean_reversion_v1", "dca_dips_v1"],
    "BEARISH":            ["funding_arb_v1", "dca_dips_v1"],   # mean_rev blocked, cvd controlled by daily_loss_limit
    "STAGFLATION":        ["funding_arb_v1", "dca_dips_v1"],
    "PARABOLIC_BULLISH":  ["funding_arb_v1"],                  # only delta-neutral safe here
}


@dataclass
class OrchestraConfig:
    """Global orchestra configuration."""
    total_capital_usd: float
    paper_mode: bool = True
    enabled_strategies: List[str] = field(default_factory=list)
    # safety caps
    max_total_drawdown_pct: float = 0.10        # halt all strategies if portfolio DD ≥ 10%
    max_per_symbol_position_pct: float = 0.20   # max 20% of total capital on one symbol


@dataclass
class ExecutionDecision:
    """Final decision after orchestration. Sent to executor (paper or live)."""
    signal: Signal
    allowed: bool
    veto_reason: Optional[str] = None
    allocated_size_usd: float = 0.0
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class StrategyOrchestra:
    """
    Coordinates multiple strategies. The single entry point per tick is:

        decisions = orchestra.run_tick(market_data_per_symbol, regime)

    Each decision is then handed to the executor (Bybit live or paper).
    """

    def __init__(self, config: OrchestraConfig):
        self.config = config
        self._strategies: Dict[str, BaseStrategy] = {}

        # Cross-strategy state
        self._symbol_total_exposure_usd: Dict[str, float] = {}
        self._portfolio_peak_value: float = config.total_capital_usd
        self._portfolio_current_value: float = config.total_capital_usd
        self._kill_switch_engaged: bool = False
        self._kill_switch_reason: Optional[str] = None

        # Tick stats
        self._total_ticks: int = 0
        self._total_signals_evaluated: int = 0
        self._total_signals_executed: int = 0

    # ---- registration ----
    def register(self, strategy: BaseStrategy) -> None:
        """Add a strategy to the orchestra."""
        sid = strategy.get_strategy_id()
        if sid in self._strategies:
            raise ValueError(f"Strategy {sid} already registered")
        self._strategies[sid] = strategy
        logger.info("Registered strategy: %s", sid)

    def get_strategy(self, strategy_id: str) -> Optional[BaseStrategy]:
        return self._strategies.get(strategy_id)

    # ---- main coordination loop ----
    def run_tick(
        self,
        market_data_per_symbol: Dict[str, Dict[str, Any]],
        regime: str,
    ) -> List[ExecutionDecision]:
        """
        One coordination tick.

        For each registered strategy, for each symbol in its universe,
        call evaluate(), apply risk gates, then orchestrate cross-strategy
        decisions.

        Returns list of ExecutionDecisions. Caller (main.py) hands them
        to the appropriate executor (paper or live).
        """
        self._total_ticks += 1

        if self._kill_switch_engaged:
            logger.warning("Tick skipped — kill switch engaged: %s", self._kill_switch_reason)
            return []

        # 1. Sync capital allocations to each strategy
        self._sync_capital_allocations()

        # 2. Apply regime-based strategy enabling
        self._apply_regime_matrix(regime)

        # 3. Collect signals from all enabled strategies
        all_signals: List[Signal] = []
        for sid, strat in self._strategies.items():
            if strat.status not in (StrategyStatus.PAPER, StrategyStatus.LIVE):
                continue

            for symbol in strat.get_universe():
                if symbol not in market_data_per_symbol:
                    continue
                self._total_signals_evaluated += 1
                try:
                    raw_signal = strat.evaluate(symbol, market_data_per_symbol[symbol], regime)
                    gated = strat.apply_risk_gates(raw_signal, regime)
                    all_signals.append(gated)
                except Exception as e:
                    logger.exception(
                        "Strategy %s eval failed on %s: %s", sid, symbol, e
                    )

        # 4. Resolve cross-strategy conflicts
        resolved = self._resolve_conflicts(all_signals)

        # 5. Apply orchestra-level caps (per-symbol, total drawdown)
        decisions = self._apply_orchestra_caps(resolved)

        # 6. Stats
        executed = sum(1 for d in decisions if d.allowed and d.signal.is_actionable())
        self._total_signals_executed += executed

        logger.debug(
            "Tick %d: %d signals → %d resolved → %d executed | regime=%s",
            self._total_ticks, len(all_signals), len(resolved), executed, regime,
        )
        return decisions

    # ---- helpers ----
    def _sync_capital_allocations(self) -> None:
        """Push current capital allocation to each strategy."""
        for sid, strat in self._strategies.items():
            allocation = self.config.total_capital_usd * strat.config.capital_pct
            # Strategies have a setter — call it via duck typing if available
            setter = getattr(strat, "set_strategy_capital", None)
            if callable(setter):
                setter(allocation)

    def _apply_regime_matrix(self, regime: str) -> None:
        """Enable/disable strategies based on REGIME_STRATEGY_MATRIX."""
        allowed = set(REGIME_STRATEGY_MATRIX.get(regime, []))
        # Also union with explicitly-enabled
        allowed.update(self.config.enabled_strategies)

        for sid, strat in self._strategies.items():
            if not strat.config.enabled:
                continue
            if strat.status == StrategyStatus.HALTED:
                continue
            if strat.status == StrategyStatus.COOLDOWN:
                continue

            if sid in allowed:
                if strat.status == StrategyStatus.DISABLED:
                    strat.set_status(
                        StrategyStatus.LIVE if not self.config.paper_mode
                        else StrategyStatus.PAPER
                    )
            else:
                # Regime disallows — disable but only if we don't have open positions
                positions = getattr(strat, "_active_positions", {})
                if not positions:
                    strat.set_status(StrategyStatus.DISABLED)

    def _resolve_conflicts(self, signals: List[Signal]) -> List[Signal]:
        """
        Group signals by symbol. If multiple strategies signal on same symbol:
        - All HOLD → keep first HOLD (no execution anyway)
        - Single actionable → keep
        - Same-direction multi-signal → keep highest confidence
        - Opposite-direction → cancel BOTH (safety: one of the strategies is wrong)
        """
        by_symbol: Dict[str, List[Signal]] = {}
        for s in signals:
            by_symbol.setdefault(s.symbol, []).append(s)

        out: List[Signal] = []
        for symbol, sig_list in by_symbol.items():
            actionable = [s for s in sig_list if s.is_actionable()]
            if not actionable:
                # All HOLD — return one representative
                out.append(sig_list[0])
                continue

            # Check for opposite-direction conflict
            longs = [s for s in actionable if s.signal_type in (SignalType.ENTER_LONG, SignalType.SCALE_IN)]
            shorts = [s for s in actionable if s.signal_type == SignalType.ENTER_SHORT]
            exits = [s for s in actionable if s.signal_type in (SignalType.EXIT, SignalType.REDUCE)]

            # Exits always pass (closing positions is safer than opening)
            for exit_sig in exits:
                out.append(exit_sig)

            if longs and shorts:
                # Conflict — cancel both, log warning
                logger.warning(
                    "Conflict on %s: %d longs vs %d shorts — both cancelled",
                    symbol, len(longs), len(shorts)
                )
                for s in longs + shorts:
                    cancelled = Signal(
                        strategy_id=s.strategy_id,
                        symbol=s.symbol,
                        signal_type=SignalType.HOLD,
                        confidence=0.0,
                        size_usd=0.0,
                        reason=f"orchestra_cancel_conflict|orig={s.signal_type.value}",
                    )
                    out.append(cancelled)
                continue

            # Same-direction: pick highest confidence
            same_dir = longs if longs else shorts
            if same_dir:
                best = max(same_dir, key=lambda s: s.confidence)
                out.append(best)

        return out

    def _apply_orchestra_caps(self, signals: List[Signal]) -> List[ExecutionDecision]:
        """Apply portfolio-level caps and produce execution decisions."""
        decisions: List[ExecutionDecision] = []

        # Drawdown kill switch check
        if self.config.total_capital_usd > 0:
            current_dd = (
                self._portfolio_peak_value - self._portfolio_current_value
            ) / self._portfolio_peak_value
            if current_dd >= self.config.max_total_drawdown_pct:
                self.engage_kill_switch(
                    f"portfolio_dd_breach|dd={current_dd:.4f}"
                )
                # All actionable signals vetoed
                for s in signals:
                    decisions.append(ExecutionDecision(
                        signal=s,
                        allowed=False,
                        veto_reason="portfolio_drawdown_kill_switch",
                    ))
                return decisions

        # Per-symbol exposure cap
        for sig in signals:
            if not sig.is_actionable():
                decisions.append(ExecutionDecision(
                    signal=sig, allowed=True, allocated_size_usd=0.0
                ))
                continue

            current_exposure = self._symbol_total_exposure_usd.get(sig.symbol, 0.0)
            max_per_symbol = (
                self.config.total_capital_usd * self.config.max_per_symbol_position_pct
            )

            # If this signal would exceed cap, downsize to fit
            available = max(max_per_symbol - current_exposure, 0.0)
            if available <= 0:
                decisions.append(ExecutionDecision(
                    signal=sig,
                    allowed=False,
                    veto_reason=f"per_symbol_cap_full|exp={current_exposure:.2f}",
                ))
                continue

            allocated = min(sig.size_usd, available)
            if allocated < 5:                  # below min meaningful size
                decisions.append(ExecutionDecision(
                    signal=sig,
                    allowed=False,
                    veto_reason="size_below_min_after_cap",
                ))
                continue

            decisions.append(ExecutionDecision(
                signal=sig,
                allowed=True,
                allocated_size_usd=allocated,
            ))

        return decisions

    # ---- portfolio state callbacks ----
    def update_portfolio_value(self, new_value_usd: float) -> None:
        self._portfolio_current_value = new_value_usd
        if new_value_usd > self._portfolio_peak_value:
            self._portfolio_peak_value = new_value_usd

    def update_symbol_exposure(self, symbol: str, exposure_usd: float) -> None:
        if exposure_usd <= 0:
            self._symbol_total_exposure_usd.pop(symbol, None)
        else:
            self._symbol_total_exposure_usd[symbol] = exposure_usd

    def engage_kill_switch(self, reason: str) -> None:
        if self._kill_switch_engaged:
            return
        self._kill_switch_engaged = True
        self._kill_switch_reason = reason
        logger.critical("KILL SWITCH ENGAGED: %s", reason)
        # Halt all strategies
        for strat in self._strategies.values():
            strat.set_status(StrategyStatus.HALTED)

    def release_kill_switch(self, manual_override_reason: str) -> None:
        """Manual override only. Should be triggered by /resume Telegram command."""
        self._kill_switch_engaged = False
        self._kill_switch_reason = None
        logger.warning("Kill switch released: %s", manual_override_reason)

    # ---- introspection ----
    def get_status(self) -> Dict[str, Any]:
        strategies_status = {
            sid: s.get_status_dict() for sid, s in self._strategies.items()
        }
        return {
            "total_capital_usd": self.config.total_capital_usd,
            "portfolio_value_usd": round(self._portfolio_current_value, 2),
            "portfolio_peak_usd": round(self._portfolio_peak_value, 2),
            "drawdown_pct": (
                round(
                    (self._portfolio_peak_value - self._portfolio_current_value)
                    / max(self._portfolio_peak_value, 1e-9),
                    4,
                )
                if self._portfolio_peak_value > 0 else 0.0
            ),
            "kill_switch": {
                "engaged": self._kill_switch_engaged,
                "reason": self._kill_switch_reason,
            },
            "paper_mode": self.config.paper_mode,
            "ticks_processed": self._total_ticks,
            "signals_evaluated": self._total_signals_evaluated,
            "signals_executed": self._total_signals_executed,
            "strategies": strategies_status,
            "symbol_exposures": dict(self._symbol_total_exposure_usd),
        }
