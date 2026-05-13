"""
strategies/funding_arb.py — Funding Rate Arbitrage v1.0

Delta-neutral strategy: capture funding rate payments without directional risk.

Mechanics:
  POSITIVE FUNDING (longs pay shorts → we want to be short the perp):
    - Open: BUY spot (e.g. ETH/USDT) + SELL perp (ETHUSDT) same notional
    - We receive funding every 8h while position open
    - Close: SELL spot + BUY perp when funding falls below close threshold

  NEGATIVE FUNDING (shorts pay longs → we want to be long the perp):
    - Open: SELL spot (need spot ETH first) + BUY perp same notional
    - This requires holding ETH spot already (we don't short spot on Bybit
      retail). For initial deployment: only handle POSITIVE funding side.

Initial scope (v1.0):
  - POSITIVE-only (long spot + short perp)
  - Paper mode by default — no real orders until LIVE_TRADING=true
  - Three pairs: ETHUSDT, SOLUSDT (BTC excluded per QA v2.3.2 rule)
  - Risk Kernel approves every position open
  - Records every leg to PnL ledger

Calibration (TBD after baseline data, ~14 days):
  - Open threshold: TBD by funding history p75
  - Close threshold: TBD by funding history p25
  - Position size: $200-300 per leg ($400-600 total exposure)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..core.bybit_client import BybitClient, FundingRate, Ticker
from ..core.pnl_ledger import PnLLedger, TradeFill
from ..core.risk_kernel import RiskKernel, TradeRequest, TradeDecision

log = logging.getLogger("qa_bot.strategies.funding_arb")


# =============================================================================
# CONFIGURATION — calibrated per DeepSeek Task #9 (2026-04-27)
#
# DeepSeek baseline (12-month BTC/ETH/SOL data):
#   BTC: mean +0.0042%/8h, p75 +0.0089%, p95 +0.0215%, % > 0.05% = 4.2%
#   ETH: mean +0.0058%/8h, p75 +0.0112%, p95 +0.0247%, % > 0.05% = 5.8%
#   SOL: mean +0.0121%/8h, p75 +0.0189%, p95 +0.0402%, % > 0.05% = 12.7%
#
# DeepSeek recommended open=0.028%/8h. We override to 0.040% per pair-default
# because 0.028% is exactly break-even per their own cost model
# (Total_Cost ≈ 0.255%, breakeven 3d = 0.028%/8h). Operating at break-even
# means zero expected edge — all variance is noise. We require 1.5x margin.
#
# Per-pair thresholds reflect different liquidity profiles.
# =============================================================================

# Conservative defaults (override per-pair below)
DEFAULT_OPEN_RATE_THRESHOLD  = 0.0004    # 0.040%/8h ≈ 44% APR
DEFAULT_CLOSE_RATE_THRESHOLD = 0.00015   # 0.015%/8h ≈ 16% APR

# Per-symbol fine-tuning (override DEFAULT_*)
PER_SYMBOL_THRESHOLDS = {
    # ETH: most balanced — open at 1.5x breakeven
    "ETHUSDT": {"open": 0.00040, "close": 0.00012, "max_basis_pct": 1.5},
    # SOL: higher rates but wider slippage — needs higher open threshold
    "SOLUSDT": {"open": 0.00050, "close": 0.00015, "max_basis_pct": 2.0},
}

# Per-leg position size (in USD). Two legs = 2x this is total exposure.
DEFAULT_LEG_SIZE_USD         = 200.0

# Min hold period — DeepSeek recommendation: 3 settlements before close eval
MIN_HOLD_HOURS               = 24.0      # 3 settlements × 8h

# Max hold — DeepSeek recommendation: re-evaluate after 14 days
MAX_HOLD_HOURS               = 14 * 24   # 336h

# Cost model (Bybit linear perps + spot, VIP-0)
# Spot taker = 0.10%, perp taker = 0.055%, slippage estimate
SPOT_TAKER_FEE               = 0.0010    # 0.10%
PERP_TAKER_FEE               = 0.00055   # 0.055%
PERP_MAKER_FEE               = 0.00020   # 0.020% (limit post-only — future use)
SLIPPAGE_ETH                 = 0.0004    # 0.04% per leg (DeepSeek estimate)
SLIPPAGE_SOL                 = 0.0008    # 0.08% per leg (wider)
TAX_PROVISION                = 0.0003    # 0.03% Latvia 20% PIT provision

# Total round-trip cost (4 transactions: open spot + open perp + close spot + close perp)
# Per DeepSeek breakeven analysis: ~0.255% for ETH, ~0.30% for SOL
def calc_round_trip_cost(symbol: str, use_maker_perp: bool = False) -> float:
    """Calculate total round-trip cost as a fraction of leg notional."""
    perp_fee = PERP_MAKER_FEE if use_maker_perp else PERP_TAKER_FEE
    if symbol == "ETHUSDT":
        slip = SLIPPAGE_ETH
    elif symbol == "SOLUSDT":
        slip = SLIPPAGE_SOL
    else:
        slip = SLIPPAGE_ETH  # default
    # 2× spot fees + 2× perp fees + 4× slippage + tax provision
    return (2 * SPOT_TAKER_FEE) + (2 * perp_fee) + (4 * slip) + TAX_PROVISION

# Whitelist (BTC excluded per QA v2.3.2, even though DeepSeek ranks it 3rd)
ALLOWED_SYMBOLS              = {"ETHUSDT", "SOLUSDT"}

# Risk caps (per DeepSeek Task #9 recommendation)
MAX_CONCURRENT_ARBS          = 2
MAX_POSITION_SIZE_PCT        = 0.20      # 20% of equity per arb


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class ArbPositionState(Enum):
    PROPOSED  = "proposed"     # Generated, awaiting confirmation
    OPENING   = "opening"      # Orders sent
    OPEN      = "open"
    CLOSING   = "closing"
    CLOSED    = "closed"
    FAILED    = "failed"


@dataclass
class ArbPosition:
    """A single delta-neutral funding arb position (spot leg + perp leg)."""
    arb_id:                str           # Internal UUID
    symbol_perp:           str           # 'ETHUSDT'
    symbol_spot:           str           # 'ETH/USDT'
    state:                 ArbPositionState
    direction:             str           # 'POSITIVE' (long spot + short perp)

    # Sizing
    spot_quantity:         float
    perp_quantity:         float

    # Prices
    entry_funding_rate:    float
    entry_perp_price:      float
    entry_spot_price:      float
    exit_funding_rate:     float = 0.0
    exit_perp_price:       float = 0.0
    exit_spot_price:       float = 0.0

    # PnL components
    funding_collected_usd: float = 0.0
    fees_paid_usd:         float = 0.0
    realized_pnl_usd:      float = 0.0

    # Timestamps
    opened_at_utc:         str   = ""
    closed_at_utc:         str   = ""

    # Mode
    is_paper:              bool  = True

    def to_telegram(self) -> str:
        icon = {
            ArbPositionState.PROPOSED:  "📝",
            ArbPositionState.OPENING:   "⏳",
            ArbPositionState.OPEN:      "🟢",
            ArbPositionState.CLOSING:   "⏳",
            ArbPositionState.CLOSED:    "✅",
            ArbPositionState.FAILED:    "🔴",
        }.get(self.state, "⚪")

        mode = "📝 PAPER" if self.is_paper else "💰 LIVE"

        if self.state == ArbPositionState.OPEN:
            hours_open = (
                (datetime.now(timezone.utc).timestamp() -
                 datetime.fromisoformat(self.opened_at_utc.replace("Z", "+00:00")).timestamp())
                / 3600
            ) if self.opened_at_utc else 0
            return (
                f"{icon} *Funding Arb* — `{self.symbol_perp}`\n"
                f"State: `{self.state.value}` ({mode})\n"
                f"Open for: `{hours_open:.1f}h`\n"
                f"Entry funding: `{self.entry_funding_rate*100:+.4f}%/8h`\n"
                f"Spot: `{self.spot_quantity:.4f}@${self.entry_spot_price:,.2f}`\n"
                f"Perp: `-{self.perp_quantity:.4f}@${self.entry_perp_price:,.2f}`\n"
                f"Funding collected: `${self.funding_collected_usd:+.4f}`"
            )
        elif self.state == ArbPositionState.CLOSED:
            return (
                f"{icon} *Funding Arb CLOSED* — `{self.symbol_perp}`\n"
                f"Mode: {mode}\n"
                f"Funding earned: `${self.funding_collected_usd:+.4f}`\n"
                f"Fees: `${self.fees_paid_usd:.4f}`\n"
                f"Net PnL: `${self.realized_pnl_usd:+.4f}`"
            )
        else:
            return f"{icon} *Funding Arb* — `{self.symbol_perp}` `{self.state.value}`"


# =============================================================================
# STRATEGY
# =============================================================================

class FundingArbStrategy:
    def get_universe(self) -> list[str]:
        """Symbols this strategy operates on (orchestra interface)."""
        return sorted(ALLOWED_SYMBOLS)

    def get_status_dict(self) -> dict:
        """Status snapshot for /strategies Telegram command (orchestra interface)."""
        status = "LIVE" if self.live_trading else "PAPER"
        capital_approx = (self.leg_size_usd * 2) / max(self.risk_kernel.current_equity, 1.0)
        return {
            "status": status,
            "daily_pnl_usd": 0.0,
            "active_positions": len(self._open_arbs),
            "capital_pct": min(capital_approx, 1.0),
            "signals_emitted": 0,
            "signals_gated": 0,
        }

    @property
    def config(self):
        """Orchestra interface — mimics StrategyConfig minimal contract."""
        from types import SimpleNamespace
        capital_approx = (self.leg_size_usd * 2) / max(self.risk_kernel.current_equity, 1.0)
        return SimpleNamespace(
            strategy_id="funding_arb_v1",
            capital_pct=min(capital_approx, 1.0),
            enabled=True,
        )

    @property
    def status(self):
        """Orchestra interface — DISABLED so orchestra.run_tick skips this strategy.
        FundingArb has its own scheduler job (funding_arb_eval), should not be
        double-evaluated via orchestra tick. /strategies display uses
        get_status_dict() which returns PAPER independently."""
        from bot.strategies.base_strategy import StrategyStatus
        return StrategyStatus.DISABLED

    def set_status(self, new_status):
        """No-op — FundingArb manages own state, ignore orchestra status changes."""
        pass

    def get_strategy_id(self) -> str:
        """Required by Orchestra.register() — returns unique strategy ID."""
        return getattr(self, "NAME", "funding_arb_v1")

    """
    Delta-neutral funding rate arbitrage.

    Core decision: should we open a new arb on this symbol given current funding?
    """

    def __init__(
        self,
        ledger:          PnLLedger,
        risk_kernel:     RiskKernel,
        open_threshold:  float = DEFAULT_OPEN_RATE_THRESHOLD,
        close_threshold: float = DEFAULT_CLOSE_RATE_THRESHOLD,
        leg_size_usd:    float = DEFAULT_LEG_SIZE_USD,
        live_trading:    bool  = False,
    ):
        self.ledger          = ledger
        self.risk_kernel     = risk_kernel
        self.open_threshold  = open_threshold
        self.close_threshold = close_threshold
        self.leg_size_usd    = leg_size_usd
        self.live_trading    = live_trading

        # In-memory tracking of open arbs (also persisted via ledger positions)
        self._open_arbs: dict[str, ArbPosition] = {}

        log.info(
            f"FundingArbStrategy initialised: "
            f"open_thr={open_threshold*100:.4f}%/8h "
            f"close_thr={close_threshold*100:.4f}%/8h "
            f"leg_size=${leg_size_usd} "
            f"live={live_trading}"
        )

    # ── DECISION ────────────────────────────────────────────────────────────────

    def should_open(
        self, fr: FundingRate, current_equity_usd: float
    ) -> tuple[bool, list[str]]:
        """
        Decision: should we open a new arb on this funding rate?
        Returns (should_open, list_of_reasons).
        """
        reasons = []

        # Whitelist
        if fr.symbol not in ALLOWED_SYMBOLS:
            reasons.append(f"Symbol {fr.symbol} not in whitelist")
            return False, reasons

        # Concurrent position limit
        active_count = sum(
            1 for arb in self._open_arbs.values()
            if arb.state in (ArbPositionState.OPEN, ArbPositionState.OPENING)
        )
        if active_count >= MAX_CONCURRENT_ARBS:
            reasons.append(f"Max concurrent arbs reached ({active_count}/{MAX_CONCURRENT_ARBS})")
            return False, reasons

        # Already have open arb on this symbol?
        if fr.symbol in self._open_arbs:
            existing = self._open_arbs[fr.symbol]
            if existing.state in (ArbPositionState.OPEN, ArbPositionState.OPENING):
                reasons.append(f"Already have open arb on {fr.symbol}")
                return False, reasons

        # Per-symbol threshold (or default)
        symbol_cfg = PER_SYMBOL_THRESHOLDS.get(fr.symbol, {})
        open_thr = symbol_cfg.get("open", self.open_threshold)

        # Threshold check (positive funding only in v1.0)
        if fr.funding_rate < open_thr:
            reasons.append(
                f"Funding {fr.funding_rate*100:.4f}%/8h below threshold "
                f"{open_thr*100:.4f}%/8h"
            )
            return False, reasons

        # Economic check: assume 3-day holding (9 settlements) at current rate.
        # Per DeepSeek Task #9: round-trip cost is symbol-specific.
        ASSUMED_HOLDING_SETTLEMENTS = 9
        round_trip_cost_pct = calc_round_trip_cost(fr.symbol, use_maker_perp=False)
        expected_funding   = self.leg_size_usd * fr.funding_rate * ASSUMED_HOLDING_SETTLEMENTS
        cost_round_trip    = self.leg_size_usd * round_trip_cost_pct
        if expected_funding < cost_round_trip * 1.5:    # 1.5x safety margin
            reasons.append(
                f"Expected 3d funding ${expected_funding:.4f} < "
                f"1.5x round-trip cost ${cost_round_trip*1.5:.4f} "
                f"(cost_pct={round_trip_cost_pct*100:.3f}%)"
            )
            return False, reasons

        # Risk cap check: position size must fit within MAX_POSITION_SIZE_PCT of equity
        max_allowed = current_equity_usd * MAX_POSITION_SIZE_PCT
        if self.leg_size_usd > max_allowed:
            reasons.append(
                f"Leg size ${self.leg_size_usd} exceeds {MAX_POSITION_SIZE_PCT*100:.0f}% "
                f"of equity (${max_allowed:.2f})"
            )
            return False, reasons

        return True, ["All checks passed"]

    def should_close(
        self, arb: ArbPosition, current_funding_rate: float
    ) -> tuple[bool, list[str]]:
        """
        Decision: should we close an open arb position?
        """
        reasons = []

        if arb.state != ArbPositionState.OPEN:
            return False, [f"Not open (state={arb.state.value})"]

        # Min hold check
        hours_open = 0.0
        if arb.opened_at_utc:
            opened_ts = datetime.fromisoformat(
                arb.opened_at_utc.replace("Z", "+00:00")
            ).timestamp()
            hours_open = (datetime.now(timezone.utc).timestamp() - opened_ts) / 3600

        if hours_open < MIN_HOLD_HOURS:
            return False, [f"Min hold {MIN_HOLD_HOURS}h not met ({hours_open:.1f}h)"]

        # Max hold reached — force close to re-evaluate
        if hours_open > MAX_HOLD_HOURS:
            reasons.append(
                f"Max hold {MAX_HOLD_HOURS}h exceeded ({hours_open:.1f}h) — force close"
            )
            return True, reasons

        # Per-symbol close threshold
        symbol_cfg = PER_SYMBOL_THRESHOLDS.get(arb.symbol_perp, {})
        close_thr = symbol_cfg.get("close", self.close_threshold)

        # Funding rate dropped below close threshold
        if current_funding_rate <= close_thr:
            reasons.append(
                f"Funding {current_funding_rate*100:.4f}%/8h below close threshold "
                f"{close_thr*100:.4f}%/8h"
            )
            return True, reasons

        # Funding flipped negative — must close (we'd start paying)
        if current_funding_rate < 0:
            reasons.append("Funding flipped negative — must close")
            return True, reasons

        return False, [
            f"Funding still healthy at {current_funding_rate*100:+.4f}%/8h "
            f"(close_thr={close_thr*100:.4f}%/8h, hours_open={hours_open:.1f})"
        ]

    # ── EXECUTION (paper mode by default) ───────────────────────────────────────

    async def open_arb(
        self,
        client:     BybitClient,
        symbol:     str,
        funding:    FundingRate,
    ) -> Optional[ArbPosition]:
        """
        Open a new delta-neutral position.
        Paper mode: simulates fills using current ticker prices.
        Live mode: NOT YET IMPLEMENTED — requires API keys + Task #8 verify.
        """
        # Risk Kernel pre-approval
        request = TradeRequest(
            asset=symbol,
            side="long",            # Spot leg is long; perp leg short (delta-neutral)
            proposed_size_usd=self.leg_size_usd,
            stop_loss_pct=0.10,     # Conservative ATR-based default; tune later
            take_profit_pct=0.0,    # No TP for arb — funding-driven exit
            strategy_name="funding_arb",
            confidence=0.85,
            market_regime="UNKNOWN",
            s13_active=False,
            vix_level=20.0,
        )
        approval = self.risk_kernel.approve_trade(request)
        if approval.decision in (TradeDecision.REJECTED, TradeDecision.HALTED):
            log.warning(
                f"Risk Kernel rejected funding_arb open: "
                f"{', '.join(approval.rejection_reasons)}"
            )
            return None

        approved_size = approval.approved_size_usd

        # Get current spot + perp prices for paper fill
        spot_symbol = symbol.replace("USDT", "/USDT")
        try:
            perp_ticker = await client.fetch_ticker(symbol, category="linear")
            spot_ticker = await client.fetch_ticker(spot_symbol, category="spot")
        except Exception as e:
            log.error(f"Failed to fetch prices for {symbol} arb: {e}")
            return None

        # Quantity from notional
        spot_qty = approved_size / spot_ticker.last_price
        perp_qty = approved_size / perp_ticker.last_price

        arb = ArbPosition(
            arb_id=str(uuid.uuid4())[:8],
            symbol_perp=symbol,
            symbol_spot=spot_symbol,
            state=ArbPositionState.OPEN if not self.live_trading else ArbPositionState.OPENING,
            direction="POSITIVE",
            spot_quantity=spot_qty,
            perp_quantity=perp_qty,
            entry_funding_rate=funding.funding_rate,
            entry_perp_price=perp_ticker.last_price,
            entry_spot_price=spot_ticker.last_price,
            opened_at_utc=datetime.now(timezone.utc).isoformat(),
            is_paper=not self.live_trading,
        )

        # Record both legs to ledger
        spot_fee = approved_size * SPOT_TAKER_FEE
        perp_fee = approved_size * PERP_TAKER_FEE

        self.ledger.record_fill(TradeFill(
            fill_time_utc=arb.opened_at_utc,
            category="spot",
            asset=spot_symbol,
            side="buy",
            quantity=spot_qty,
            price=spot_ticker.last_price,
            fee_usd=spot_fee,
            fill_id=f"arb_{arb.arb_id}_spot_open",
            strategy="funding_arb",
            is_paper=arb.is_paper,
        ))
        self.ledger.record_fill(TradeFill(
            fill_time_utc=arb.opened_at_utc,
            category="linear",
            asset=symbol,
            side="sell",
            quantity=perp_qty,
            price=perp_ticker.last_price,
            fee_usd=perp_fee,
            fill_id=f"arb_{arb.arb_id}_perp_open",
            strategy="funding_arb",
            is_paper=arb.is_paper,
        ))

        arb.fees_paid_usd = spot_fee + perp_fee
        self._open_arbs[symbol] = arb

        if self.live_trading:
            log.warning(
                "LIVE TRADING REQUESTED but order routing not yet implemented. "
                "Position recorded as OPENING — manual intervention required."
            )

        log.info(
            f"Funding arb opened: {arb.arb_id} {symbol} "
            f"spot={spot_qty:.4f}@${spot_ticker.last_price:,.2f} "
            f"perp=-{perp_qty:.4f}@${perp_ticker.last_price:,.2f} "
            f"fees=${arb.fees_paid_usd:.4f} mode={'PAPER' if arb.is_paper else 'LIVE'}"
        )
        return arb

    async def close_arb(
        self,
        client:        BybitClient,
        arb:           ArbPosition,
        current_funding: float,
    ) -> bool:
        """Close both legs of an arb position."""
        if arb.state != ArbPositionState.OPEN:
            log.warning(f"Cannot close arb {arb.arb_id}: state={arb.state.value}")
            return False

        try:
            perp_ticker = await client.fetch_ticker(arb.symbol_perp, category="linear")
            spot_ticker = await client.fetch_ticker(arb.symbol_spot, category="spot")
        except Exception as e:
            log.error(f"Close fetch failed for {arb.arb_id}: {e}")
            return False

        arb.state              = ArbPositionState.CLOSING
        arb.exit_funding_rate  = current_funding
        arb.exit_perp_price    = perp_ticker.last_price
        arb.exit_spot_price    = spot_ticker.last_price
        arb.closed_at_utc      = datetime.now(timezone.utc).isoformat()

        # Spot exit notional + fees
        spot_exit_notional = arb.spot_quantity * spot_ticker.last_price
        perp_exit_notional = arb.perp_quantity * perp_ticker.last_price
        spot_close_fee     = spot_exit_notional * SPOT_TAKER_FEE
        perp_close_fee     = perp_exit_notional * PERP_TAKER_FEE

        self.ledger.record_fill(TradeFill(
            fill_time_utc=arb.closed_at_utc,
            category="spot", asset=arb.symbol_spot, side="sell",
            quantity=arb.spot_quantity, price=spot_ticker.last_price,
            fee_usd=spot_close_fee,
            fill_id=f"arb_{arb.arb_id}_spot_close",
            strategy="funding_arb", is_paper=arb.is_paper,
        ))
        self.ledger.record_fill(TradeFill(
            fill_time_utc=arb.closed_at_utc,
            category="linear", asset=arb.symbol_perp, side="buy",
            quantity=arb.perp_quantity, price=perp_ticker.last_price,
            fee_usd=perp_close_fee,
            fill_id=f"arb_{arb.arb_id}_perp_close",
            strategy="funding_arb", is_paper=arb.is_paper,
        ))

        # Realised PnL components
        spot_pnl = (spot_ticker.last_price - arb.entry_spot_price) * arb.spot_quantity
        perp_pnl = (arb.entry_perp_price - perp_ticker.last_price) * arb.perp_quantity
        # spot_pnl + perp_pnl ≈ 0 (delta-neutral). Difference = basis convergence.
        arb.fees_paid_usd     += spot_close_fee + perp_close_fee
        arb.realized_pnl_usd  = (
            spot_pnl + perp_pnl + arb.funding_collected_usd - arb.fees_paid_usd
        )
        arb.state             = ArbPositionState.CLOSED

        # Update Risk Kernel PnL tracker
        self.risk_kernel.record_trade_outcome(arb.realized_pnl_usd, arb.symbol_perp)

        del self._open_arbs[arb.symbol_perp]

        log.info(
            f"Funding arb closed: {arb.arb_id} {arb.symbol_perp} "
            f"funding=${arb.funding_collected_usd:+.4f} "
            f"fees=${arb.fees_paid_usd:.4f} "
            f"PnL=${arb.realized_pnl_usd:+.4f}"
        )
        return True

    # ── MAIN LOOP HOOK ──────────────────────────────────────────────────────────

    async def evaluate_cycle(
        self,
        client:        BybitClient,
        funding_rates: list[FundingRate],
    ):
        """
        Single evaluation cycle. Called periodically by scheduler.

        For each known funding rate:
          1. If we have open arb on this symbol — check if should close
          2. If we don't — check if should open
        """
        for fr in funding_rates:
            if fr.symbol in self._open_arbs:
                arb = self._open_arbs[fr.symbol]
                close_decision, reasons = self.should_close(arb, fr.funding_rate)
                if close_decision:
                    log.info(f"Closing arb {arb.arb_id}: {', '.join(reasons)}")
                    await self.close_arb(client, arb, fr.funding_rate)
                continue

            open_decision, reasons = self.should_open(
                fr, self.risk_kernel.current_equity
            )
            if open_decision:
                log.info(f"Opening arb on {fr.symbol}: {', '.join(reasons)}")
                await self.open_arb(client, fr.symbol, fr)

    def get_open_arbs(self) -> list[ArbPosition]:
        return list(self._open_arbs.values())


# =============================================================================
# CLI / TEST
# =============================================================================

if __name__ == "__main__":
    """Quick decision logic test (no network)."""
    import logging
    import tempfile
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with tempfile.TemporaryDirectory() as td:
        ledger = PnLLedger(Path(td) / "test_pnl.db")
        kernel = RiskKernel(starting_equity_usd=1000.0)
        strat  = FundingArbStrategy(
            ledger=ledger, risk_kernel=kernel, live_trading=False
        )

        print(f"\n{'='*70}")
        print("FundingArbStrategy decision logic test")
        print(f"{'='*70}\n")

        # Test cases
        cases = [
            FundingRate("BTCUSDT", 0.0010, 0, time.time()),  # Excluded (BTC)
            FundingRate("ETHUSDT", 0.0010, 0, time.time()),  # Open: high rate
            FundingRate("ETHUSDT", 0.0001, 0, time.time()),  # Skip: low rate
            FundingRate("SOLUSDT", -0.0008, 0, time.time()), # Skip: negative
            FundingRate("SOLUSDT", 0.0008, 0, time.time()),  # Open: high
        ]

        for fr in cases:
            should, reasons = strat.should_open(fr, current_equity_usd=1000.0)
            decision = "✅ OPEN" if should else "❌ SKIP"
            print(f"{decision}  {fr.symbol}  rate={fr.funding_rate*100:+.4f}%/8h")
            for r in reasons:
                print(f"     → {r}")

        print(f"\n{'='*70}")
        print("✅ Decision logic test complete")
        print(f"{'='*70}")
