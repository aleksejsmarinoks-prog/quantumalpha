"""
core/earn_manager.py — QuantumAlpha Earn Manager v1.1

Bybit Earn position tracker with optional auto-subscription mode.

Modes:
  - READ_ONLY (default): user subscribes manually via Bybit UI,
    records to bot via /earn_add command. Safe.
  - AUTO (LIVE_EARN_MODE=true): bot can subscribe via verified
    /v5/earn/place-order endpoint (DeepSeek Task #8 verified).
    Requires API key with Earn permission.

Live mode covers (per DeepSeek Task #8 verification):
  ✅ Flexible Savings (USDT, USDC, etc.) — full subscribe/redeem
  ✅ On-Chain Earn / Staking — full subscribe/redeem
  ⚠️ Fixed-Term — same FlexibleSaving API, but no early redeem
  ❌ Liquidity Mining — read-only via API (no place-order endpoint)
  ❌ Dual Asset / Discount Buy / Launchpool — manual UI required

Capital safety: live mode requires explicit env flag AND per-call confirmation
in Telegram before any subscribe action (no fully autonomous capital deployment).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

from .pnl_ledger import PnLLedger, EarnPosition

if TYPE_CHECKING:
    from .bybit_client import BybitClient

log = logging.getLogger("qa_bot.earn_manager")


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class EarnSummary:
    """Aggregate snapshot of all active Earn positions."""
    total_positions:        int
    total_principal_usd:    float
    total_interest_usd:     float
    blended_apr:            float       # weighted by principal
    by_product:             dict        # {'flexible_savings': $X, ...}
    by_coin:                dict        # {'USDT': $X, ...}
    expiring_soon:          list        # positions with <7 days to maturity
    expired_unredeemed:     list        # positions past maturity, not redeemed

    def to_telegram(self) -> str:
        lines = [
            f"💰 *Earn Layer Status*",
            f"Total: `${self.total_principal_usd:,.2f}` "
            f"across `{self.total_positions}` positions",
            f"Blended APR: `{self.blended_apr*100:.2f}%`",
            f"Interest YTD: `${self.total_interest_usd:,.2f}`",
            "",
            "*By product:*",
        ]
        for product, amount in self.by_product.items():
            lines.append(f"  • {product}: `${amount:,.2f}`")

        lines.append("")
        lines.append("*By coin:*")
        for coin, amount in self.by_coin.items():
            lines.append(f"  • {coin}: `${amount:,.2f}`")

        if self.expiring_soon:
            lines.append("")
            lines.append("⚠️ *Expiring within 7 days:*")
            for p in self.expiring_soon:
                lines.append(
                    f"  • {p['coin']} ${p['principal']:,.2f} "
                    f"@ {p['apr']*100:.1f}% — exp {p['days_remaining']:.0f}d"
                )

        if self.expired_unredeemed:
            lines.append("")
            lines.append("🔴 *Expired but NOT redeemed:*")
            for p in self.expired_unredeemed:
                lines.append(f"  • {p['coin']} ${p['principal']:,.2f} — REDEEM NOW")

        return "\n".join(lines)


@dataclass
class TargetAllocation:
    """Recommended capital allocation across Earn products."""
    coin:               str
    product_type:       str
    target_amount_usd:  float
    target_apr:         float
    rationale:          str


# Default target allocation for $25K passive layer
DEFAULT_ALLOCATION_25K = [
    TargetAllocation(
        coin="USDT", product_type="flexible_savings",
        target_amount_usd=500.0, target_apr=0.12,
        rationale="Tier-1 cap (max 12% APR), instant liquidity",
    ),
    TargetAllocation(
        coin="USDT", product_type="flexible_savings",
        target_amount_usd=4500.0, target_apr=0.01,
        rationale="Tier-2/3 (~1% APR), instant liquidity buffer",
    ),
    TargetAllocation(
        coin="USDC", product_type="fixed_term",
        target_amount_usd=8000.0, target_apr=0.12,
        rationale="7-15d ladder (4×$2K), avg 10-15% APR",
    ),
    TargetAllocation(
        coin="USDT", product_type="fixed_term",
        target_amount_usd=8000.0, target_apr=0.10,
        rationale="30-90d for capital not needed short-term",
    ),
    TargetAllocation(
        coin="ETH", product_type="onchain_staking",
        target_amount_usd=2000.0, target_apr=0.04,
        rationale="If holding ETH spot, free yield via staking",
    ),
    TargetAllocation(
        coin="USDT", product_type="reserve",
        target_amount_usd=2000.0, target_apr=0.0,
        rationale="DCA + emergency margin top-up",
    ),
]


# =============================================================================
# EARN MANAGER
# =============================================================================

class EarnManager:
    """
    Manual or auto-subscription Earn tracker. Reads from PnL ledger.

    User flow (read-only mode):
      1. Subscribe on Bybit UI manually
      2. /earn_add USDT 500 flexible_savings 0.12  (in Telegram)
      3. EarnManager records to ledger
      4. Periodically: /earn_status returns blended APR, interest earned, etc

    User flow (auto mode, requires LIVE_EARN_MODE=true):
      1. Bot lists products via /v5/earn/product
      2. User authorises subscribe via /earn_subscribe Telegram command
      3. Bot calls /v5/earn/place-order with HMAC-signed request
      4. Records to ledger automatically

    Per DeepSeek Task #8: only FlexibleSaving and OnChain categories
    have working place-order API. Fixed-term, Dual Asset, Discount Buy,
    Launchpool require manual UI subscription.
    """

    # Bybit API category strings (per DeepSeek Task #8)
    BYBIT_CATEGORY_FLEXIBLE = "FlexibleSaving"
    BYBIT_CATEGORY_ONCHAIN  = "OnChain"

    # Internal product types (used in our DB) → Bybit categories
    AUTOMATABLE_PRODUCTS = {
        "flexible_savings":  BYBIT_CATEGORY_FLEXIBLE,
        "onchain_staking":   BYBIT_CATEGORY_ONCHAIN,
    }
    # These require manual UI subscription:
    MANUAL_ONLY_PRODUCTS = {
        "fixed_term",        # Same API but Bybit doesn't separate it cleanly
        "dual_asset",
        "discount_buy",
        "launchpool",
        "reserve",
    }

    def __init__(
        self,
        ledger:        PnLLedger,
        bybit_client:  Optional["BybitClient"] = None,
        live_mode:     bool = False,
    ):
        self.ledger        = ledger
        self.bybit_client  = bybit_client
        self.live_mode     = live_mode and (bybit_client is not None)

        if live_mode and bybit_client is None:
            log.warning(
                "EarnManager: live_mode requested but no bybit_client provided. "
                "Falling back to read-only mode."
            )
            self.live_mode = False

        log.info(
            f"EarnManager initialised: live_mode={self.live_mode} "
            f"(client={'present' if bybit_client else 'absent'})"
        )

    # ── ADD / UPDATE ────────────────────────────────────────────────────────────

    def add_position(
        self,
        coin:           str,
        principal:      float,
        product_type:   str,           # 'flexible_savings' | 'fixed_term' | 'onchain_staking' | 'reserve'
        apr:            float,         # 0.12 = 12% annualized
        term_days:      Optional[int] = None,
        notes:          Optional[str] = None,
    ) -> int:
        """Record a manually-subscribed Earn position. Returns ledger row ID."""
        position = EarnPosition(
            subscribed_utc=datetime.now(timezone.utc).isoformat(),
            product_type=product_type,
            coin=coin.upper(),
            principal=principal,
            apr=apr,
            term_days=term_days,
            notes=notes,
        )
        row_id = self.ledger.record_earn_subscription(position)
        log.info(
            f"Earn position added: #{row_id} {product_type} {coin} "
            f"${principal:,.2f} @ {apr*100:.2f}% APR"
        )
        return row_id

    def record_interest(
        self,
        earn_position_id:   int,
        coin:               str,
        interest_amount:    float,
        coin_usd_price:     float = 1.0,    # 1.0 for stablecoins
    ) -> int:
        """Record an interest payment for an active Earn position."""
        interest_usd = interest_amount * coin_usd_price
        return self.ledger.record_earn_interest(
            earn_position_id=earn_position_id,
            coin=coin.upper(),
            interest_amount=interest_amount,
            interest_usd=interest_usd,
        )

    # ── SUMMARY / REPORTING ─────────────────────────────────────────────────────

    def get_summary(self) -> EarnSummary:
        """Aggregate snapshot of all active Earn positions."""
        positions = self.ledger.get_active_earn_positions()

        if not positions:
            return EarnSummary(
                total_positions=0, total_principal_usd=0.0,
                total_interest_usd=0.0, blended_apr=0.0,
                by_product={}, by_coin={},
                expiring_soon=[], expired_unredeemed=[],
            )

        # Aggregate
        total_principal     = sum(p["principal"] for p in positions)
        total_interest      = sum(p.get("interest_earned", 0) or 0 for p in positions)
        weighted_apr_num    = sum(p["principal"] * p["apr"] for p in positions)
        blended_apr         = weighted_apr_num / total_principal if total_principal > 0 else 0.0

        by_product: dict = {}
        by_coin:    dict = {}
        for p in positions:
            by_product[p["product_type"]] = by_product.get(p["product_type"], 0) + p["principal"]
            by_coin[p["coin"]]            = by_coin.get(p["coin"], 0) + p["principal"]

        # Expiring soon (Fixed-Term positions within 7 days of maturity)
        now = datetime.now(timezone.utc)
        expiring_soon       = []
        expired_unredeemed  = []
        for p in positions:
            if p.get("term_days") and p["term_days"] > 0:
                try:
                    sub_dt = datetime.fromisoformat(p["subscribed_utc"].replace("Z", "+00:00"))
                    maturity = sub_dt + timedelta(days=p["term_days"])
                    days_remaining = (maturity - now).total_seconds() / 86400
                    p_with_days = {**p, "days_remaining": days_remaining}
                    if days_remaining < 0:
                        expired_unredeemed.append(p_with_days)
                    elif days_remaining < 7:
                        expiring_soon.append(p_with_days)
                except Exception:
                    pass

        return EarnSummary(
            total_positions=len(positions),
            total_principal_usd=total_principal,
            total_interest_usd=total_interest,
            blended_apr=blended_apr,
            by_product=by_product,
            by_coin=by_coin,
            expiring_soon=expiring_soon,
            expired_unredeemed=expired_unredeemed,
        )

    def gap_analysis(
        self, target: list[TargetAllocation] = None
    ) -> list[dict]:
        """
        Compare current Earn allocation vs target. Returns prioritized gaps.
        Useful for /earn_plan command to show what user needs to subscribe.
        """
        target = target or DEFAULT_ALLOCATION_25K
        positions = self.ledger.get_active_earn_positions()

        # Aggregate current allocation by (coin, product_type)
        current: dict = {}
        for p in positions:
            key = (p["coin"], p["product_type"])
            current[key] = current.get(key, 0) + p["principal"]

        gaps = []
        for t in target:
            key = (t.coin, t.product_type)
            current_amount = current.get(key, 0)
            gap_usd        = t.target_amount_usd - current_amount
            gaps.append({
                "coin":             t.coin,
                "product_type":     t.product_type,
                "target_usd":       t.target_amount_usd,
                "current_usd":      current_amount,
                "gap_usd":          gap_usd,
                "filled_pct":       (current_amount / t.target_amount_usd * 100)
                                    if t.target_amount_usd > 0 else 0,
                "target_apr":       t.target_apr,
                "rationale":        t.rationale,
            })
        # Largest gaps first
        gaps.sort(key=lambda g: -g["gap_usd"])
        return gaps

    # ── HELPERS ─────────────────────────────────────────────────────────────────

    @staticmethod
    def estimate_daily_earnings(principal: float, apr: float) -> float:
        """Daily interest accrual estimate."""
        return principal * apr / 365

    @staticmethod
    def estimate_monthly_earnings(principal: float, apr: float) -> float:
        return principal * apr / 12

    # ── LIVE API METHODS (gated behind live_mode flag) ──────────────────────────

    async def list_available_products(
        self, category: str = BYBIT_CATEGORY_FLEXIBLE, coin: Optional[str] = None
    ) -> list[dict]:
        """
        Query Bybit for available Earn products.
        GET /v5/earn/product (no auth required)

        Per DeepSeek Task #8: returns list with productId, estimateApr,
        coin, minStakeAmount, maxStakeAmount, status.
        """
        if not self.bybit_client:
            log.warning("list_available_products called without bybit_client")
            return []
        try:
            return await self.bybit_client.list_earn_products(
                category=category, coin=coin
            )
        except Exception as e:
            log.error(f"list_available_products error: {e}")
            return []

    async def subscribe_live(
        self,
        product_id:   str,
        amount:       float,
        coin:         str,
        category:     str = BYBIT_CATEGORY_FLEXIBLE,
        account_type: str = "FUND",
        order_link_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Place a live Earn subscription via /v5/earn/place-order.

        REQUIRES:
        - live_mode=True
        - bybit_client with API key + Earn permission
        - User explicit confirmation upstream (Telegram)

        Returns: order result dict with orderId, or None on failure.
        """
        if not self.live_mode:
            log.error("subscribe_live called but live_mode=False")
            return None
        if not self.bybit_client:
            log.error("subscribe_live called without bybit_client")
            return None

        if order_link_id is None:
            order_link_id = f"qa-{coin.lower()}-{int(datetime.now(timezone.utc).timestamp())}"

        log.info(
            f"LIVE Earn subscribe: {category} productId={product_id} "
            f"amount={amount} {coin} account={account_type}"
        )
        try:
            result = await self.bybit_client.place_earn_order(
                category=category,
                order_type="Stake",
                account_type=account_type,
                amount=str(amount),
                coin=coin,
                product_id=product_id,
                order_link_id=order_link_id,
            )
            return result
        except Exception as e:
            log.error(f"subscribe_live failed: {e}")
            return None

    async def redeem_live(
        self,
        product_id:    str,
        amount:        float,
        coin:          str,
        category:      str = BYBIT_CATEGORY_FLEXIBLE,
        account_type:  str = "FUND",
        order_link_id: Optional[str] = None,
        redeem_position_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Place a live Earn redemption via /v5/earn/place-order with orderType=Redeem.
        """
        if not self.live_mode or not self.bybit_client:
            log.error("redeem_live called but live_mode disabled")
            return None

        if order_link_id is None:
            order_link_id = f"qa-redeem-{coin.lower()}-{int(datetime.now(timezone.utc).timestamp())}"

        log.info(
            f"LIVE Earn redeem: {category} productId={product_id} "
            f"amount={amount} {coin}"
        )
        try:
            result = await self.bybit_client.place_earn_order(
                category=category,
                order_type="Redeem",
                account_type=account_type,
                amount=str(amount),
                coin=coin,
                product_id=product_id,
                order_link_id=order_link_id,
                redeem_position_id=redeem_position_id,
            )
            return result
        except Exception as e:
            log.error(f"redeem_live failed: {e}")
            return None

    @classmethod
    def is_automatable(cls, product_type: str) -> bool:
        """True if this product type can be auto-subscribed via API."""
        return product_type in cls.AUTOMATABLE_PRODUCTS

    async def check_apr_and_alert(self, bot, chat_id) -> None:
        """Phase 7.3 stub — APR drift alerting not yet implemented.

        Caller in bot/scheduler.py:154 expects this method. Returning
        no-op silences AttributeError noise (~4/day). Real implementation
        deferred to Phase 7.5+: should compare current blended APR
        (from get_summary()) against historical baseline and alert via
        `bot.send_message(chat_id, ...)` on drift > threshold.
        """
        return None


# =============================================================================
# CLI / TEST
# =============================================================================

if __name__ == "__main__":
    """Smoke test using temp ledger."""
    import tempfile, logging
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with tempfile.TemporaryDirectory() as td:
        ledger = PnLLedger(Path(td) / "test_pnl.db")
        em = EarnManager(ledger=ledger)

        print(f"\n{'='*70}")
        print("EarnManager smoke test")
        print(f"{'='*70}\n")

        # Simulate: user subscribed manually, reports to bot
        em.add_position("USDT", 500.0, "flexible_savings", 0.12,
                        notes="Tier-1 max APR")
        em.add_position("USDT", 4500.0, "flexible_savings", 0.01,
                        notes="Tier-2 buffer")
        em.add_position("USDC", 2000.0, "fixed_term", 0.12, term_days=7,
                        notes="Ladder slot 1")
        em.add_position("USDC", 2000.0, "fixed_term", 0.13, term_days=15,
                        notes="Ladder slot 2")

        summary = em.get_summary()
        print("Summary:")
        print(summary.to_telegram())

        print(f"\n--- Daily earnings estimates: ---")
        print(f"  $500 @ 12%:    ${em.estimate_daily_earnings(500, 0.12):.4f}/day")
        print(f"  $4500 @ 1%:    ${em.estimate_daily_earnings(4500, 0.01):.4f}/day")
        print(f"  $2000 @ 12%:   ${em.estimate_daily_earnings(2000, 0.12):.4f}/day")
        print(f"  Total daily:   "
              f"${summary.total_principal_usd * summary.blended_apr / 365:.4f}/day")

        print(f"\n--- Gap analysis vs $25K target: ---")
        gaps = em.gap_analysis()
        for g in gaps:
            status = "✅" if g["filled_pct"] >= 100 else "⚠️" if g["filled_pct"] > 0 else "❌"
            print(f"  {status} {g['coin']} {g['product_type']}: "
                  f"${g['current_usd']:>8.0f} / ${g['target_usd']:>8.0f} "
                  f"({g['filled_pct']:>5.1f}%) — gap ${g['gap_usd']:>8.0f}")
            print(f"      → {g['rationale']}")

        print(f"\n{'='*70}")
        print("✅ EarnManager test complete")
        print(f"{'='*70}")
