"""
BacktestRiskKernel v2 — production-equivalent risk kernel for walk-forward replay.

Phase 6.3.1 — Production Bridge for Backtester.
Build: 2026-05-14 (post-audit, real production constants applied)

Replaces:
  - MockRiskKernel always-allow (suggested in Phase 6.3.1 promt — wrong)
  - BacktestRiskKernel v1 (had incorrect 1% per-trade limits)

This v2 mirrors `bot/core/risk_kernel.py:RiskKernel` exactly, with:
  - Same TradeRequest / TradeApproval API (drop-in compatible)
  - Same constants (verified from production source)
  - In-memory state (no JSON persistence)
  - Injected clock (no time.time() / datetime.now() calls)
  - reset() per walk-forward window

Authority: `bot/core/risk_kernel.py` is the production source. Any
discrepancy → production wins. This file is a backtest mirror.

Author: Claude (Project advisor, QA Phase 6.3.1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional


# =============================================================================
# CONSTANTS — Mirror of bot/core/risk_kernel.py (DO NOT DIVERGE)
# =============================================================================

MAX_POSITION_PCT_OF_EQUITY = 0.25
MAX_TOTAL_LEVERAGE         = 3.0
MAX_RISK_PER_TRADE_PCT     = 0.02
DEFAULT_RISK_PER_TRADE_PCT = 0.01

DAILY_DD_LIMIT_PCT    = 0.05
WEEKLY_DD_LIMIT_PCT   = 0.10
TOTAL_DD_LIMIT_PCT    = 0.15

CONSECUTIVE_LOSS_COOLDOWN_TRIGGER = 3
CONSECUTIVE_LOSS_COOLDOWN_HOURS   = 24

S13_POSITION_MULT      = 0.50
HIGH_VIX_MULT          = 0.70           # VIX > 30
ELEVATED_VIX_MULT      = 0.85           # VIX > 25

MIN_ORDER_USD = 10.0


# =============================================================================
# ENUMS — Mirror of production
# =============================================================================

class TradeDecision(Enum):
    APPROVED     = "approved"
    REJECTED     = "rejected"
    REDUCED_SIZE = "reduced_size"
    HALTED       = "halted"


class HaltReason(Enum):
    NONE             = "none"
    DAILY_DD         = "daily_drawdown"
    WEEKLY_DD        = "weekly_drawdown"
    TOTAL_DD         = "total_drawdown"
    CONSECUTIVE_LOSS = "consecutive_loss_cooldown"
    MANUAL           = "manual_halt"
    EXCHANGE_FAILURE = "exchange_api_failure"
    KILLSWITCH       = "killswitch"


# =============================================================================
# DATA STRUCTURES — Mirror of production
# =============================================================================

@dataclass
class TradeRequest:
    asset:              str
    side:               str
    proposed_size_usd:  float
    stop_loss_pct:      float
    take_profit_pct:    float
    strategy_name:      str
    confidence:         float
    market_regime:      str
    s13_active:         bool
    vix_level:          float
    current_leverage:   float = 0.0


@dataclass
class TradeApproval:
    decision:           TradeDecision
    approved_size_usd:  float
    halt_reason:        HaltReason
    rejection_reasons:  list
    warnings:           list
    metadata:           dict = field(default_factory=dict)
    timestamp_utc:      str  = ""


# =============================================================================
# CLOCK PROTOCOL
# =============================================================================

class WallClock:
    """Default clock — uses wall time. Same as production."""
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class BacktestClock:
    """Backtest clock — advances by external tick."""

    def __init__(self, initial: Optional[datetime] = None) -> None:
        self._current: datetime = initial or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def set(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._current = ts


# =============================================================================
# BACKTEST RISK KERNEL
# =============================================================================

class BacktestRiskKernel:
    """
    Production-mirror RiskKernel for walk-forward backtest.

    Behavioral equivalence with `bot.core.risk_kernel.RiskKernel`:
      - Same TradeRequest / TradeApproval contract
      - Same constants
      - Same approve_trade() decision tree (Layer 1-5)
      - Same killswitch logic (daily/weekly/total DD, consec loss)
      - Same stress multipliers (S13, VIX)

    Differences from production:
      - Clock is injectable (BacktestClock) — required for replay
      - No JSON persistence
      - reset() for walk-forward window boundaries
      - Audit trail in-memory (block_log, fill_log, approval_log)
    """

    def __init__(
        self,
        starting_equity_usd: float,
        clock: Optional[object] = None,
    ) -> None:
        self.clock = clock or WallClock()
        self.starting_equity_usd_initial = starting_equity_usd

        self.starting_equity = starting_equity_usd
        self.current_equity  = starting_equity_usd
        self.peak_equity     = starting_equity_usd
        self._daily_pnl: float = 0.0
        self._weekly_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._halted: bool = False
        self._halt_reason: HaltReason = HaltReason.NONE
        self._halt_until: Optional[datetime] = None

        now = self.clock.now()
        self._daily_anchor: datetime = self._next_utc_midnight(now)
        self._weekly_anchor: datetime = self._next_utc_monday(now)

        self.block_log: list = []
        self.fill_log: list = []
        self.approval_log: list = []

    # ─── Public API (mirror of production) ─────────────────────────────────

    def approve_trade(self, req: TradeRequest) -> TradeApproval:
        """Main entry point. Mirror of production approve_trade()."""
        self._maybe_reset_periods()

        rejections: list = []
        warnings: list = []

        # ── Layer 1: Killswitch ────────────────────────────────────────────
        active, halt_reason = self._check_killswitch_active()
        if not active:
            return self._log_and_return(self._make_approval(
                TradeDecision.HALTED, 0.0, halt_reason,
                [f"Trading halted: {halt_reason.value}"], [],
            ))

        # ── Layer 2: Validation ────────────────────────────────────────────
        if req.proposed_size_usd <= 0:
            rejections.append("Proposed size must be positive")
        if req.stop_loss_pct <= 0 or req.stop_loss_pct > 0.25:
            rejections.append(
                f"Stop-loss {req.stop_loss_pct*100:.1f}% outside sane range (0 < SL ≤ 25%)"
            )
        if req.confidence < 0.0 or req.confidence > 1.0:
            rejections.append(f"Confidence {req.confidence} outside [0,1]")

        if rejections:
            return self._reject(rejections, warnings)

        # ── Layer 3: Position sizing ───────────────────────────────────────
        risk_pct = DEFAULT_RISK_PER_TRADE_PCT
        if req.s13_active:
            risk_pct *= S13_POSITION_MULT
            warnings.append("S13 active: risk per trade reduced 50%")
        elif req.vix_level >= 30:
            risk_pct *= HIGH_VIX_MULT
            warnings.append(f"VIX {req.vix_level:.1f} > 30: risk reduced 30%")
        elif req.vix_level >= 25:
            risk_pct *= ELEVATED_VIX_MULT
            warnings.append(f"VIX {req.vix_level:.1f} > 25: risk reduced 15%")

        risk_dollars      = self.current_equity * risk_pct
        risk_based_size   = risk_dollars / req.stop_loss_pct
        max_position_size = self.current_equity * MAX_POSITION_PCT_OF_EQUITY

        approved_size = min(req.proposed_size_usd, risk_based_size, max_position_size)

        if approved_size < req.proposed_size_usd * 0.5:
            warnings.append(
                f"Size reduced to ${approved_size:,.2f} from ${req.proposed_size_usd:,.2f} "
                f"(risk_based=${risk_based_size:,.2f}, max=${max_position_size:,.2f})"
            )

        if approved_size < MIN_ORDER_USD:
            rejections.append(f"Approved size ${approved_size:,.2f} below Bybit minimum ${MIN_ORDER_USD}")
            return self._reject(rejections, warnings)

        # ── Layer 4: Leverage ──────────────────────────────────────────────
        leverage_after = req.current_leverage + (approved_size / self.current_equity)
        if leverage_after > MAX_TOTAL_LEVERAGE:
            rejections.append(
                f"Total leverage {leverage_after:.2f}x would exceed {MAX_TOTAL_LEVERAGE}x cap"
            )
            return self._reject(rejections, warnings)

        # ── Layer 5: R:R sanity (warning only) ────────────────────────────
        if req.take_profit_pct > 0:
            rr = req.take_profit_pct / req.stop_loss_pct
            if rr < 1.0:
                warnings.append(
                    f"R:R {rr:.2f} below 1:1 — strategy must compensate via win rate"
                )

        # ── Approved ───────────────────────────────────────────────────────
        decision = (TradeDecision.APPROVED if approved_size == req.proposed_size_usd
                    else TradeDecision.REDUCED_SIZE)

        return self._log_and_return(self._make_approval(
            decision, round(approved_size, 2), HaltReason.NONE, [], warnings,
            metadata={
                "applied_risk_pct":   risk_pct,
                "risk_dollars":       round(risk_dollars, 2),
                "max_position_size":  round(max_position_size, 2),
                "current_equity":     round(self.current_equity, 2),
            },
        ))

    def record_trade_outcome(self, pnl_usd: float, asset: str = "") -> None:
        """Update PnL state. Mirror of production. Triggers killswitch checks."""
        self._daily_pnl  += pnl_usd
        self._weekly_pnl += pnl_usd
        self._total_pnl  += pnl_usd
        self.current_equity = self.starting_equity + self._total_pnl

        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity

        if pnl_usd < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        self._evaluate_killswitches()

        self.fill_log.append({
            "timestamp": self.clock.now().isoformat(),
            "asset": asset,
            "pnl_usd": pnl_usd,
            "equity_after": self.current_equity,
        })

    def is_trading_allowed(self) -> bool:
        active, _ = self._check_killswitch_active()
        return active

    def get_status(self) -> dict:
        active, halt_reason = self._check_killswitch_active()
        return {
            "equity":             round(self.current_equity, 2),
            "starting_equity":    round(self.starting_equity, 2),
            "peak_equity":        round(self.peak_equity, 2),
            "daily_pnl":          round(self._daily_pnl, 2),
            "weekly_pnl":         round(self._weekly_pnl, 2),
            "total_pnl":          round(self._total_pnl, 2),
            "total_dd_pct":       round(
                (self.peak_equity - self.current_equity) / self.peak_equity * 100, 2
            ) if self.peak_equity > 0 else 0.0,
            "consecutive_losses": self._consecutive_losses,
            "halted":             not active,
            "halt_reason":        halt_reason.value,
            "halt_until_utc":     self._halt_until.isoformat() if self._halt_until else None,
            "approvals_count":    len(self.approval_log),
            "blocks_count":       len(self.block_log),
            "fills_count":        len(self.fill_log),
        }

    # ─── Backtest-specific API ─────────────────────────────────────────────

    def reset(self, starting_equity_usd: Optional[float] = None) -> None:
        """Reset all state. Called at start of each walk-forward window."""
        equity = starting_equity_usd if starting_equity_usd is not None else self.starting_equity_usd_initial
        self.starting_equity = equity
        self.current_equity  = equity
        self.peak_equity     = equity
        self._daily_pnl      = 0.0
        self._weekly_pnl     = 0.0
        self._total_pnl      = 0.0
        self._consecutive_losses = 0
        self._halted         = False
        self._halt_reason    = HaltReason.NONE
        self._halt_until     = None

        now = self.clock.now()
        self._daily_anchor   = self._next_utc_midnight(now)
        self._weekly_anchor  = self._next_utc_monday(now)

        self.block_log.clear()
        self.fill_log.clear()
        self.approval_log.clear()

    def maybe_reset_periods(self) -> None:
        """Public hook — replay engine calls each bar after clock.set()."""
        self._maybe_reset_periods()

    # ─── Private (mirror of production) ────────────────────────────────────

    def _check_killswitch_active(self) -> tuple:
        if self._halted:
            now = self.clock.now()
            if self._halt_until is not None and now >= self._halt_until:
                self._halted = False
                self._halt_reason = HaltReason.NONE
                self._halt_until = None
                return True, HaltReason.NONE
            return False, self._halt_reason
        return True, HaltReason.NONE

    def _evaluate_killswitches(self) -> None:
        if self.current_equity <= 0:
            return

        # Daily DD
        daily_dd = abs(min(self._daily_pnl, 0)) / self.current_equity
        if daily_dd >= DAILY_DD_LIMIT_PCT:
            self._trigger_halt(HaltReason.DAILY_DD, hours=24)
            return

        # Weekly DD
        weekly_dd = abs(min(self._weekly_pnl, 0)) / self.current_equity
        if weekly_dd >= WEEKLY_DD_LIMIT_PCT:
            self._trigger_halt(HaltReason.WEEKLY_DD, hours=168)
            return

        # Total DD from peak
        if self.peak_equity > 0:
            total_dd = (self.peak_equity - self.current_equity) / self.peak_equity
            if total_dd >= TOTAL_DD_LIMIT_PCT:
                self._trigger_halt(HaltReason.TOTAL_DD, hours=999999)
                return

        # Consecutive losses
        if self._consecutive_losses >= CONSECUTIVE_LOSS_COOLDOWN_TRIGGER:
            self._trigger_halt(HaltReason.CONSECUTIVE_LOSS, hours=CONSECUTIVE_LOSS_COOLDOWN_HOURS)
            self._consecutive_losses = 0

    def _trigger_halt(self, reason: HaltReason, hours: float) -> None:
        self._halted      = True
        self._halt_reason = reason
        now = self.clock.now()
        self._halt_until  = now + timedelta(hours=hours)
        self.block_log.append({
            "timestamp": now.isoformat(),
            "type": "killswitch",
            "reason": reason.value,
            "halt_until": self._halt_until.isoformat(),
        })

    def _maybe_reset_periods(self) -> None:
        now = self.clock.now()
        if now >= self._daily_anchor:
            self._daily_pnl = 0.0
            self._daily_anchor = self._next_utc_midnight(now)
        if now >= self._weekly_anchor:
            self._weekly_pnl = 0.0
            self._weekly_anchor = self._next_utc_monday(now)

    def _reject(self, reasons: list, warnings: list) -> TradeApproval:
        approval = self._make_approval(
            TradeDecision.REJECTED, 0.0, HaltReason.NONE, reasons, warnings,
        )
        self.block_log.append({
            "timestamp": self.clock.now().isoformat(),
            "type": "reject",
            "reasons": reasons,
        })
        self.approval_log.append(approval)
        return approval

    def _make_approval(
        self, decision: TradeDecision, approved_size: float, halt_reason: HaltReason,
        rejections: list, warnings: list, metadata: Optional[dict] = None,
    ) -> TradeApproval:
        return TradeApproval(
            decision=decision,
            approved_size_usd=approved_size,
            halt_reason=halt_reason,
            rejection_reasons=rejections,
            warnings=warnings,
            metadata=metadata or {},
            timestamp_utc=self.clock.now().isoformat(),
        )

    def _log_and_return(self, approval: TradeApproval) -> TradeApproval:
        self.approval_log.append(approval)
        return approval

    @staticmethod
    def _next_utc_midnight(now: datetime) -> datetime:
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return tomorrow

    @staticmethod
    def _next_utc_monday(now: datetime) -> datetime:
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        return now + timedelta(days=days_until_monday)
