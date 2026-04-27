"""
core/risk_kernel.py — QuantumAlpha Risk Kernel v1.0

Hard limits and kill switches for Bybit prop trading.
This is the CRITICAL FILE — all trades must pass through it.

Responsibilities:
  1. Pre-trade gates: position sizing, leverage caps, exposure limits
  2. Multi-layer kill switches: daily/weekly/total drawdown
  3. Cooldown after consecutive losses
  4. DeepSeek anti-pattern detection (self-audit)
  5. Strategy health monitoring
  6. Shadow evaluation framework for parameter changes

Design principle: this kernel has VETO power over every trade.
No strategy, no signal, no override can bypass these checks.

Author: QuantumAlpha team
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger("qa_bot.risk_kernel")


# =============================================================================
# CONSTANTS — Hard Limits (immutable, no optimization can bypass)
# =============================================================================

# Capital protection
MAX_POSITION_PCT_OF_EQUITY = 0.25       # 25% — single position cap
MAX_TOTAL_LEVERAGE         = 3.0        # 3x — total account leverage cap
MAX_RISK_PER_TRADE_PCT     = 0.02       # 2% — max risk per single trade
DEFAULT_RISK_PER_TRADE_PCT = 0.01       # 1% — default risk per trade

# Drawdown limits (compound failsafe — any breach halts trading)
DAILY_DD_LIMIT_PCT    = 0.05            # 5% — daily drawdown halts trading 24h
WEEKLY_DD_LIMIT_PCT   = 0.10            # 10% — weekly drawdown halts trading 7d
TOTAL_DD_LIMIT_PCT    = 0.15            # 15% — total drawdown halts pending review

# Cooldown after consecutive losses (DeepSeek Pattern 4: Hard Safety Constraints)
CONSECUTIVE_LOSS_COOLDOWN_TRIGGER = 3   # 3 losses in a row → cooldown
CONSECUTIVE_LOSS_COOLDOWN_HOURS   = 24

# Stress regime multipliers (S13 active or VIX > thresholds)
S13_POSITION_MULT      = 0.50           # Cut positions 50% in S13
HIGH_VIX_MULT          = 0.70           # VIX > 30
ELEVATED_VIX_MULT      = 0.85           # VIX > 25

# Anti-pattern thresholds (DeepSeek research insights)
SUSPICIOUS_WIN_RATE         = 0.85      # >85% WR + low DD = Martingale flag
SUSPICIOUS_DD_THRESHOLD     = 0.08      # <8% max DD with <365 days = young account flag
MIN_HEALTHY_SHARPE          = 0.8       # Below = risk disproportionate
MAX_HEALTHY_LEVERAGE_RATIO  = 8.0       # return*10/DD ratio = leverage estimate


# =============================================================================
# ENUMS
# =============================================================================

class TradeDecision(Enum):
    APPROVED       = "approved"
    REJECTED       = "rejected"
    REDUCED_SIZE   = "reduced_size"
    HALTED         = "halted"


class HaltReason(Enum):
    NONE              = "none"
    DAILY_DD          = "daily_drawdown"
    WEEKLY_DD         = "weekly_drawdown"
    TOTAL_DD          = "total_drawdown"
    CONSECUTIVE_LOSS  = "consecutive_loss_cooldown"
    MANUAL            = "manual_halt"
    EXCHANGE_FAILURE  = "exchange_api_failure"
    KILLSWITCH        = "killswitch"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class TradeRequest:
    """Pre-trade check input from any strategy."""
    asset:              str
    side:               str          # "long" / "short"
    proposed_size_usd:  float
    stop_loss_pct:      float        # Stop-loss distance from entry, %
    take_profit_pct:    float        # Take-profit distance, %
    strategy_name:      str
    confidence:         float        # 0.0 - 1.0
    market_regime:      str          # e.g., "STAGFLATION_WAR", "TRANSITION"
    s13_active:         bool
    vix_level:          float
    current_leverage:   float = 0.0  # Current account leverage including this trade


@dataclass
class TradeApproval:
    """Risk Kernel decision output."""
    decision:           TradeDecision
    approved_size_usd:  float          # 0 if rejected
    halt_reason:        HaltReason
    rejection_reasons:  list[str]
    warnings:           list[str]
    metadata:           dict           = field(default_factory=dict)
    timestamp_utc:      str            = ""

    def __post_init__(self):
        if not self.timestamp_utc:
            self.timestamp_utc = datetime.now(timezone.utc).isoformat()


@dataclass
class StrategyHealth:
    """Self-audit metrics for a strategy (DeepSeek anti-pattern detection)."""
    strategy_name:           str
    total_trades:            int
    win_rate:                float
    avg_return_pct:          float       # Avg return per trade %
    max_drawdown_pct:        float
    sharpe_ratio:            float
    consecutive_losses:      int
    days_active:             int
    capital_injection_count: int = 0     # Times user added capital after DD
    flags:                   list[str] = field(default_factory=list)


# =============================================================================
# RISK KERNEL
# =============================================================================

class RiskKernel:
    """
    Production risk control. All trades pass through approve_trade().
    State persisted to JSON for crash recovery.
    """

    def __init__(
        self,
        starting_equity_usd: float,
        state_file_path:     Optional[Path] = None,
    ):
        self.starting_equity = starting_equity_usd
        self.current_equity  = starting_equity_usd
        self.peak_equity     = starting_equity_usd

        # State
        self._daily_pnl:           float = 0.0
        self._weekly_pnl:          float = 0.0
        self._total_pnl:           float = 0.0
        self._consecutive_losses:  int   = 0
        self._halted:              bool  = False
        self._halt_reason:         HaltReason = HaltReason.NONE
        self._halt_until_ts:       float = 0.0
        self._daily_reset_ts:      float = self._next_utc_midnight_ts()
        self._weekly_reset_ts:     float = self._next_utc_monday_ts()

        # Persistence
        self.state_file = state_file_path
        if self.state_file and self.state_file.exists():
            self._load_state()

        log.info(
            f"RiskKernel initialised: equity=${starting_equity_usd:,.2f} "
            f"daily_dd_limit={DAILY_DD_LIMIT_PCT*100:.0f}% "
            f"weekly_dd_limit={WEEKLY_DD_LIMIT_PCT*100:.0f}%"
        )

    # ── PUBLIC API: TRADE APPROVAL ──────────────────────────────────────────────

    def approve_trade(self, req: TradeRequest) -> TradeApproval:
        """
        Main entry point. Every trade request passes through this.
        Returns TradeApproval with decision + sizing.
        """
        # Check time-based resets first
        self._maybe_reset_periods()

        rejections: list[str] = []
        warnings:   list[str] = []

        # ── Layer 1: Killswitch check ──────────────────────────────────────────
        active, halt_reason = self._check_killswitch_active()
        if not active:
            return TradeApproval(
                decision=TradeDecision.HALTED,
                approved_size_usd=0.0,
                halt_reason=halt_reason,
                rejection_reasons=[f"Trading halted: {halt_reason.value}"],
                warnings=[],
            )

        # ── Layer 2: Pre-trade validation ──────────────────────────────────────
        if req.proposed_size_usd <= 0:
            rejections.append("Proposed size must be positive")
        if req.stop_loss_pct <= 0 or req.stop_loss_pct > 0.25:
            rejections.append(
                f"Stop-loss {req.stop_loss_pct*100:.1f}% outside sane range "
                f"(0% < SL ≤ 25%)"
            )
        if req.confidence < 0.0 or req.confidence > 1.0:
            rejections.append(f"Confidence {req.confidence} outside [0,1]")

        if rejections:
            return self._reject(rejections, warnings)

        # ── Layer 3: Position sizing — apply risk_per_trade rule ──────────────
        risk_pct = DEFAULT_RISK_PER_TRADE_PCT
        # Reduce risk in stress conditions
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

        # Final size = min(strategy proposal, risk-based, hard cap)
        approved_size = min(req.proposed_size_usd, risk_based_size, max_position_size)

        if approved_size < req.proposed_size_usd * 0.5:
            warnings.append(
                f"Size reduced to ${approved_size:,.2f} from ${req.proposed_size_usd:,.2f} "
                f"(risk_based=${risk_based_size:,.2f}, max=${max_position_size:,.2f})"
            )

        if approved_size < 10:  # Bybit min order
            rejections.append(f"Approved size ${approved_size:,.2f} below Bybit minimum")
            return self._reject(rejections, warnings)

        # ── Layer 4: Leverage check ────────────────────────────────────────────
        if req.current_leverage + (approved_size / self.current_equity) > MAX_TOTAL_LEVERAGE:
            rejections.append(
                f"Total leverage would exceed {MAX_TOTAL_LEVERAGE}x cap"
            )
            return self._reject(rejections, warnings)

        # ── Layer 5: R:R sanity check ──────────────────────────────────────────
        if req.take_profit_pct > 0:
            rr = req.take_profit_pct / req.stop_loss_pct
            if rr < 1.0:
                warnings.append(
                    f"R:R {rr:.2f} below 1:1 — strategy must compensate via win rate"
                )

        # ── Approved ───────────────────────────────────────────────────────────
        decision = TradeDecision.APPROVED if approved_size == req.proposed_size_usd \
                   else TradeDecision.REDUCED_SIZE

        log.info(
            f"Trade approved: {req.asset} {req.side} ${approved_size:,.2f} "
            f"(req=${req.proposed_size_usd:,.2f}) strategy={req.strategy_name} "
            f"conf={req.confidence:.2f}"
        )

        return TradeApproval(
            decision=decision,
            approved_size_usd=round(approved_size, 2),
            halt_reason=HaltReason.NONE,
            rejection_reasons=[],
            warnings=warnings,
            metadata={
                "applied_risk_pct":   risk_pct,
                "risk_dollars":       round(risk_dollars, 2),
                "max_position_size":  round(max_position_size, 2),
                "current_equity":     round(self.current_equity, 2),
            },
        )

    # ── PUBLIC API: PnL TRACKING ────────────────────────────────────────────────

    def record_trade_outcome(self, pnl_usd: float, asset: str = ""):
        """
        Update PnL state after trade closes.
        Triggers killswitch checks automatically.
        """
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

        log.info(
            f"PnL recorded: {asset} ${pnl_usd:+.2f} | "
            f"daily=${self._daily_pnl:+.2f} weekly=${self._weekly_pnl:+.2f} "
            f"equity=${self.current_equity:,.2f} cons_losses={self._consecutive_losses}"
        )

        self._evaluate_killswitches()
        self._save_state()

    def update_equity_from_external(self, new_equity_usd: float):
        """
        Reconcile equity with actual exchange balance.
        Used by periodic sync to catch funding rate payments, etc.
        """
        diff = new_equity_usd - self.current_equity
        if abs(diff) > 0.01:
            log.info(f"Equity reconciliation: {diff:+.2f} USD (external sync)")
            self.current_equity = new_equity_usd
            self._total_pnl     = new_equity_usd - self.starting_equity
            if new_equity_usd > self.peak_equity:
                self.peak_equity = new_equity_usd
        self._save_state()

    # ── KILLSWITCH MANAGEMENT ───────────────────────────────────────────────────

    def manual_halt(self, hours: float = 24.0, reason: str = "manual"):
        """User-triggered halt."""
        self._halted        = True
        self._halt_reason   = HaltReason.MANUAL
        self._halt_until_ts = time.time() + (hours * 3600)
        log.warning(f"MANUAL HALT activated: {reason} ({hours}h)")
        self._save_state()

    def manual_resume(self):
        """User-triggered resume (only valid if halt is manual)."""
        if self._halt_reason != HaltReason.MANUAL:
            log.warning(f"Cannot manually resume: halt reason is {self._halt_reason.value}")
            return False
        self._halted        = False
        self._halt_reason   = HaltReason.NONE
        self._halt_until_ts = 0.0
        log.info("Trading resumed")
        self._save_state()
        return True

    def get_status(self) -> dict:
        """Snapshot for dashboard / Telegram bot."""
        active, halt_reason = self._check_killswitch_active()
        return {
            "equity":               round(self.current_equity, 2),
            "starting_equity":      round(self.starting_equity, 2),
            "peak_equity":          round(self.peak_equity, 2),
            "daily_pnl":            round(self._daily_pnl, 2),
            "weekly_pnl":           round(self._weekly_pnl, 2),
            "total_pnl":            round(self._total_pnl, 2),
            "total_dd_pct":         round(
                (self.peak_equity - self.current_equity) / self.peak_equity * 100,
                2
            ) if self.peak_equity > 0 else 0.0,
            "consecutive_losses":   self._consecutive_losses,
            "halted":               not active,
            "halt_reason":          halt_reason.value,
            "halt_until_utc":       (
                datetime.fromtimestamp(self._halt_until_ts, tz=timezone.utc).isoformat()
                if self._halt_until_ts > 0 else None
            ),
        }

    # ── ANTI-PATTERN DETECTION (DeepSeek research) ──────────────────────────────

    @staticmethod
    def audit_strategy_health(metrics: StrategyHealth) -> list[str]:
        """
        Apply DeepSeek-derived anti-pattern checks.
        Returns list of red flags. Empty list = healthy strategy.

        Patterns detected:
          1. Pre-explosion Martingale: high WR + low DD + young account
          2. Hidden leverage: return/DD ratio implies high leverage
          3. Low Sharpe: risk disproportionate to return
          4. Capital injection masking
        """
        flags = []

        # Pattern 1: Pre-explosion Martingale
        if (metrics.win_rate > SUSPICIOUS_WIN_RATE and
            metrics.max_drawdown_pct < SUSPICIOUS_DD_THRESHOLD and
            metrics.days_active < 365):
            flags.append(
                f"MARTINGALE_RISK: WR {metrics.win_rate*100:.0f}% + "
                f"DD {metrics.max_drawdown_pct*100:.1f}% + "
                f"age {metrics.days_active}d → likely pre-explosion pattern"
            )

        # Pattern 2: Implied high leverage (DeepSeek formula)
        if metrics.max_drawdown_pct > 0:
            annualized_return = metrics.avg_return_pct * (365 / max(metrics.days_active, 1))
            if annualized_return > 0:
                implied_leverage = (annualized_return * 10) / (metrics.max_drawdown_pct * 100)
                if implied_leverage > MAX_HEALTHY_LEVERAGE_RATIO:
                    flags.append(
                        f"HIGH_IMPLIED_LEVERAGE: estimated {implied_leverage:.1f}x "
                        f"(formula: ann_return*10 / DD)"
                    )

        # Pattern 3: Low Sharpe
        if 0 < metrics.sharpe_ratio < MIN_HEALTHY_SHARPE:
            flags.append(
                f"LOW_SHARPE: {metrics.sharpe_ratio:.2f} < {MIN_HEALTHY_SHARPE} "
                f"→ risk disproportionate to return"
            )

        # Pattern 4: Capital injection masking
        if metrics.capital_injection_count >= 2:
            flags.append(
                f"CAPITAL_INJECTION_MASKING: {metrics.capital_injection_count} top-ups "
                f"→ percentage drawdowns may be diluted"
            )

        return flags

    # ── PRIVATE: HELPERS ────────────────────────────────────────────────────────

    def _reject(self, reasons: list[str], warnings: list[str]) -> TradeApproval:
        log.warning(f"Trade rejected: {'; '.join(reasons)}")
        return TradeApproval(
            decision=TradeDecision.REJECTED,
            approved_size_usd=0.0,
            halt_reason=HaltReason.NONE,
            rejection_reasons=reasons,
            warnings=warnings,
        )

    def _check_killswitch_active(self) -> tuple[bool, HaltReason]:
        """Returns (active=True if trading allowed, halt_reason)."""
        if self._halted:
            if self._halt_until_ts > 0 and time.time() >= self._halt_until_ts:
                # Auto-resume after timed halt expires
                log.info(f"Halt expired, auto-resuming (was: {self._halt_reason.value})")
                self._halted        = False
                self._halt_reason   = HaltReason.NONE
                self._halt_until_ts = 0.0
                return True, HaltReason.NONE
            return False, self._halt_reason
        return True, HaltReason.NONE

    def _evaluate_killswitches(self):
        """Check all DD limits + cooldown. Halt if any breached."""
        # Daily DD
        daily_dd = abs(min(self._daily_pnl, 0)) / self.current_equity
        if daily_dd >= DAILY_DD_LIMIT_PCT:
            self._trigger_halt(HaltReason.DAILY_DD, hours=24)
            return

        # Weekly DD
        weekly_dd = abs(min(self._weekly_pnl, 0)) / self.current_equity
        if weekly_dd >= WEEKLY_DD_LIMIT_PCT:
            self._trigger_halt(HaltReason.WEEKLY_DD, hours=168)  # 7 days
            return

        # Total DD from peak
        if self.peak_equity > 0:
            total_dd = (self.peak_equity - self.current_equity) / self.peak_equity
            if total_dd >= TOTAL_DD_LIMIT_PCT:
                self._trigger_halt(HaltReason.TOTAL_DD, hours=999999)  # Indef. — manual review
                return

        # Consecutive loss cooldown
        if self._consecutive_losses >= CONSECUTIVE_LOSS_COOLDOWN_TRIGGER:
            self._trigger_halt(
                HaltReason.CONSECUTIVE_LOSS,
                hours=CONSECUTIVE_LOSS_COOLDOWN_HOURS
            )
            self._consecutive_losses = 0  # Reset after triggering

    def _trigger_halt(self, reason: HaltReason, hours: float):
        self._halted        = True
        self._halt_reason   = reason
        self._halt_until_ts = time.time() + (hours * 3600)
        log.error(
            f"🚨 KILLSWITCH TRIGGERED: {reason.value} "
            f"(halt {hours:.0f}h) "
            f"daily=${self._daily_pnl:+.2f} weekly=${self._weekly_pnl:+.2f} "
            f"equity=${self.current_equity:,.2f}"
        )

    def _maybe_reset_periods(self):
        now = time.time()
        if now >= self._daily_reset_ts:
            log.info(f"Daily reset: pnl was ${self._daily_pnl:+.2f}")
            self._daily_pnl       = 0.0
            self._daily_reset_ts  = self._next_utc_midnight_ts()
        if now >= self._weekly_reset_ts:
            log.info(f"Weekly reset: pnl was ${self._weekly_pnl:+.2f}")
            self._weekly_pnl      = 0.0
            self._weekly_reset_ts = self._next_utc_monday_ts()

    @staticmethod
    def _next_utc_midnight_ts() -> float:
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.timestamp() + 86400

    @staticmethod
    def _next_utc_monday_ts() -> float:
        now = datetime.now(timezone.utc)
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        return now.timestamp() + (days_until_monday * 86400)

    # ── PERSISTENCE ─────────────────────────────────────────────────────────────

    def _save_state(self):
        if not self.state_file:
            return
        state = {
            "starting_equity":     self.starting_equity,
            "current_equity":      self.current_equity,
            "peak_equity":         self.peak_equity,
            "daily_pnl":           self._daily_pnl,
            "weekly_pnl":          self._weekly_pnl,
            "total_pnl":           self._total_pnl,
            "consecutive_losses":  self._consecutive_losses,
            "halted":              self._halted,
            "halt_reason":         self._halt_reason.value,
            "halt_until_ts":       self._halt_until_ts,
            "daily_reset_ts":      self._daily_reset_ts,
            "weekly_reset_ts":     self._weekly_reset_ts,
            "saved_utc":           datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state, indent=2))
        except Exception as e:
            log.error(f"State save failed: {e}")

    def _load_state(self):
        try:
            data = json.loads(self.state_file.read_text())
            self.starting_equity      = data["starting_equity"]
            self.current_equity       = data["current_equity"]
            self.peak_equity          = data["peak_equity"]
            self._daily_pnl           = data["daily_pnl"]
            self._weekly_pnl          = data["weekly_pnl"]
            self._total_pnl           = data["total_pnl"]
            self._consecutive_losses  = data["consecutive_losses"]
            self._halted              = data["halted"]
            self._halt_reason         = HaltReason(data["halt_reason"])
            self._halt_until_ts       = data["halt_until_ts"]
            self._daily_reset_ts      = data["daily_reset_ts"]
            self._weekly_reset_ts     = data["weekly_reset_ts"]
            log.info(
                f"State loaded from {self.state_file} "
                f"(saved {data.get('saved_utc', 'unknown')})"
            )
        except Exception as e:
            log.warning(f"State load failed ({e}), using defaults")


# =============================================================================
# CLI / TEST HOOK
# =============================================================================

if __name__ == "__main__":
    """Quick smoke test."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    kernel = RiskKernel(
        starting_equity_usd=1000.0,
        state_file_path=Path("/tmp/qa_risk_kernel_test.json"),
    )

    # Test 1: normal trade
    req = TradeRequest(
        asset="ETH/USDT",
        side="long",
        proposed_size_usd=300.0,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        strategy_name="funding_arb",
        confidence=0.75,
        market_regime="STAGFLATION_WAR",
        s13_active=False,
        vix_level=22.0,
    )
    decision = kernel.approve_trade(req)
    print(f"\nTest 1 (normal): {decision.decision.value} "
          f"size=${decision.approved_size_usd:.2f}")
    if decision.warnings:
        print(f"  Warnings: {decision.warnings}")

    # Test 2: stress regime
    req2 = req
    req2.s13_active = True
    req2.vix_level  = 32.0
    decision = kernel.approve_trade(req2)
    print(f"\nTest 2 (S13 + high VIX): {decision.decision.value} "
          f"size=${decision.approved_size_usd:.2f}")
    print(f"  Warnings: {decision.warnings}")

    # Test 3: simulate losses → trigger killswitch
    print("\nTest 3: simulating losses to trigger daily DD killswitch...")
    kernel.record_trade_outcome(-30.0, "ETH/USDT")
    kernel.record_trade_outcome(-25.0, "SOL/USDT")
    status = kernel.get_status()
    print(f"  Status: halted={status['halted']} reason={status['halt_reason']} "
          f"daily_pnl={status['daily_pnl']}")

    # Test 4: anti-pattern detection
    print("\nTest 4: anti-pattern audit (suspicious strategy)...")
    suspicious = StrategyHealth(
        strategy_name="grid_v1",
        total_trades=200,
        win_rate=0.92,           # SUSPICIOUS
        avg_return_pct=0.5,
        max_drawdown_pct=0.05,   # SUSPICIOUS
        sharpe_ratio=0.6,        # SUSPICIOUS
        consecutive_losses=0,
        days_active=120,         # YOUNG
        capital_injection_count=2,  # MASKING
    )
    flags = RiskKernel.audit_strategy_health(suspicious)
    print(f"  Flags ({len(flags)}):")
    for f in flags:
        print(f"    - {f}")
