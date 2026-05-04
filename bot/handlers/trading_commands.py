"""
handlers/trading_commands.py — Telegram commands for prop trading layer

Commands added (separate from existing macro pipeline commands):
  /balance      — capital allocation across active/passive/reserves
  /funding      — current funding rates + opportunities + stats
  /earn         — Earn layer status, blended APR, expiring positions
  /earn_add     — manually add Earn position (after subscribing on Bybit UI)
  /earn_plan    — gap analysis vs target $25K allocation
  /halt         — manual halt all trading
  /resume       — resume after manual halt
  /paper_pnl    — paper trading PnL since start
  /positions    — open trading positions (perps + arbs)
  /risk_status  — risk kernel state, kill switches, DD

Authentication: only ALLOWED_USER_ID can use these.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

log = logging.getLogger("qa_bot.trading_commands")
router = Router()

# These are dependency-injected by bot.py at startup
_dependencies = {
    "ledger":         None,    # PnLLedger
    "risk_kernel":    None,    # RiskKernel
    "earn_manager":   None,    # EarnManager
    "funding_monitor": None,   # FundingMonitor
    "funding_arb":    None,    # FundingArbStrategy
    "bybit_client":   None,    # BybitClient (or factory)
}


def setup_trading_commands(
    ledger, risk_kernel, earn_manager, funding_monitor, funding_arb=None
):
    """Inject dependencies. Call from bot.py during startup."""
    _dependencies["ledger"]           = ledger
    _dependencies["risk_kernel"]      = risk_kernel
    _dependencies["earn_manager"]     = earn_manager
    _dependencies["funding_monitor"]  = funding_monitor
    _dependencies["funding_arb"]      = funding_arb
    log.info("Trading commands setup: dependencies injected")


def _is_authorized(user_id: int) -> bool:
    allowed = os.getenv("ALLOWED_USER_ID", "0")
    return str(user_id) == str(allowed)


def _md_escape(text: str) -> str:
    """Escape MarkdownV2 special chars for Telegram."""
    chars = r"_*[]()~`>#+-=|{}.!"
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text


# =============================================================================
# /balance — capital allocation snapshot
# =============================================================================

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    risk    = _dependencies["risk_kernel"]
    ledger  = _dependencies["ledger"]
    earn    = _dependencies["earn_manager"]

    if not (risk and ledger and earn):
        await message.answer("⚠️ Trading layer not yet initialised")
        return

    risk_status = risk.get_status()
    earn_summary = earn.get_summary()
    latest_eq    = ledger.get_latest_equity() or {}

    text = (
        f"💼 *Capital Allocation*\n\n"
        f"🎯 *Active Trading Layer*\n"
        f"  Equity: `${risk_status['equity']:,.2f}`\n"
        f"  Daily PnL: `${risk_status['daily_pnl']:+.2f}`\n"
        f"  Weekly PnL: `${risk_status['weekly_pnl']:+.2f}`\n"
        f"  Total PnL: `${risk_status['total_pnl']:+.2f}`\n"
        f"  Halted: `{risk_status['halted']}`\n\n"
        f"💰 *Passive Earn Layer*\n"
        f"  Principal: `${earn_summary.total_principal_usd:,.2f}`\n"
        f"  Blended APR: `{earn_summary.blended_apr*100:.2f}%`\n"
        f"  Positions: `{earn_summary.total_positions}`\n"
        f"  Daily yield est: "
        f"`${earn_summary.total_principal_usd * earn_summary.blended_apr / 365:.4f}`"
    )
    await message.answer(_md_escape(text), parse_mode="MarkdownV2")


# =============================================================================
# /funding — current rates + opportunities
# =============================================================================

@router.message(Command("funding"))
async def cmd_funding(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    monitor = _dependencies["funding_monitor"]
    if not monitor:
        await message.answer("⚠️ Funding monitor not initialised")
        return

    await message.answer("⏳ Fetching latest funding rates...")

    # Trigger immediate refresh
    try:
        results = await monitor.fetch_once()
    except Exception as e:
        log.error(f"funding fetch error: {e}")
        await message.answer(f"❌ Fetch failed: {e}")
        return

    if not results:
        await message.answer("No funding data available")
        return

    lines = ["📈 *Current Funding Rates*\n"]
    for fr in results:
        icon = "🔥" if abs(fr.funding_rate) >= 0.001 else \
               "⚡" if abs(fr.funding_rate) >= 0.0005 else "💤"
        lines.append(
            f"{icon} `{fr.symbol}`  "
            f"`{fr.funding_rate*100:+.4f}%/8h`  "
            f"= `{fr.annualized_pct:+.0f}% APR`"
        )

    # Add stats if available
    lines.append("")
    lines.append("📊 *7d Stats* (when available)")
    for fr in results:
        stats = monitor.get_stats(fr.symbol, days=7)
        if stats:
            lines.append(
                f"  {fr.symbol}: median `{stats.median_rate*100:+.4f}%/8h` "
                f"max `{stats.max_rate*100:+.4f}` "
                f"(N={stats.samples})"
            )
        else:
            lines.append(f"  {fr.symbol}: insufficient data (need 10+ samples)")

    text = "\n".join(lines)
    await message.answer(_md_escape(text), parse_mode="MarkdownV2")


# =============================================================================
# /earn — Earn layer status
# =============================================================================

@router.message(Command("earn"))
async def cmd_earn(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    em = _dependencies["earn_manager"]
    if not em:
        await message.answer("⚠️ Earn manager not initialised")
        return

    summary = em.get_summary()
    if summary.total_positions == 0:
        await message.answer(
            _md_escape(
                "💰 *Earn Layer*\n\n"
                "No active positions tracked.\n\n"
                "Subscribe manually on Bybit UI, then add to bot:\n"
                "`/earn_add USDT 500 flexible_savings 0.12`"
            ),
            parse_mode="MarkdownV2",
        )
        return

    await message.answer(_md_escape(summary.to_telegram()), parse_mode="MarkdownV2")


# =============================================================================
# /earn_add — manually record an Earn subscription
# =============================================================================

@router.message(Command("earn_add"))
async def cmd_earn_add(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    em = _dependencies["earn_manager"]
    if not em:
        await message.answer("⚠️ Earn manager not initialised")
        return

    parts = message.text.split()
    # /earn_add COIN AMOUNT PRODUCT_TYPE APR [TERM_DAYS]
    if len(parts) < 5:
        await message.answer(
            _md_escape(
                "*Usage:*\n"
                "`/earn_add COIN AMOUNT PRODUCT_TYPE APR [TERM_DAYS]`\n\n"
                "*Examples:*\n"
                "`/earn_add USDT 500 flexible_savings 0.12`\n"
                "`/earn_add USDC 2000 fixed_term 0.13 7`\n"
                "`/earn_add ETH 0.5 onchain_staking 0.04`\n\n"
                "*Product types:*\n"
                "  • flexible\\_savings\n"
                "  • fixed\\_term \\(needs TERM\\_DAYS\\)\n"
                "  • onchain\\_staking\n"
                "  • reserve"
            ),
            parse_mode="MarkdownV2",
        )
        return

    try:
        coin         = parts[1].upper()
        principal    = float(parts[2])
        product_type = parts[3].lower()
        apr          = float(parts[4])
        term_days    = int(parts[5]) if len(parts) >= 6 else None
    except ValueError as e:
        await message.answer(f"❌ Bad arguments: {e}")
        return

    valid_types = {"flexible_savings", "fixed_term", "onchain_staking", "reserve"}
    if product_type not in valid_types:
        await message.answer(
            f"❌ Invalid product_type. Valid: {', '.join(valid_types)}"
        )
        return

    if product_type == "fixed_term" and term_days is None:
        await message.answer("❌ fixed_term requires TERM_DAYS argument")
        return

    row_id = em.add_position(
        coin=coin, principal=principal,
        product_type=product_type, apr=apr,
        term_days=term_days,
        notes=f"Added via Telegram by user {message.from_user.id}",
    )

    daily = em.estimate_daily_earnings(principal, apr)
    text = (
        f"✅ *Earn position #{row_id} recorded*\n\n"
        f"  Coin: `{coin}`\n"
        f"  Principal: `${principal:,.2f}`\n"
        f"  Product: `{product_type}`\n"
        f"  APR: `{apr*100:.2f}%`\n"
        f"  Term: `{term_days}d`" if term_days else f"  Term: `flexible`\n"
        f"\n💵 Daily earnings est: `${daily:.4f}`"
    )
    await message.answer(_md_escape(text), parse_mode="MarkdownV2")


# =============================================================================
# /earn_plan — gap analysis vs $25K target
# =============================================================================

@router.message(Command("earn_plan"))
async def cmd_earn_plan(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    em = _dependencies["earn_manager"]
    if not em:
        await message.answer("⚠️ Earn manager not initialised")
        return

    gaps = em.gap_analysis()
    lines = ["📋 *Earn Allocation Plan vs $25K Target*\n"]

    for g in gaps:
        if g["filled_pct"] >= 100:
            status = "✅"
        elif g["filled_pct"] > 0:
            status = "⚠️"
        else:
            status = "❌"
        lines.append(
            f"{status} `{g['coin']} {g['product_type']}`\n"
            f"  Current: `${g['current_usd']:,.2f}` / "
            f"Target: `${g['target_usd']:,.2f}` ({g['filled_pct']:.0f}%)\n"
            f"  → {g['rationale']}"
        )

    text = "\n".join(lines)
    await message.answer(_md_escape(text), parse_mode="MarkdownV2")


# =============================================================================
# /halt — manual trading halt
# =============================================================================

@router.message(Command("halt"))
async def cmd_halt(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    risk = _dependencies["risk_kernel"]
    if not risk:
        await message.answer("⚠️ Risk kernel not initialised")
        return

    parts = message.text.split()
    hours = float(parts[1]) if len(parts) >= 2 else 24.0
    reason = " ".join(parts[2:]) if len(parts) >= 3 else "manual via Telegram"

    risk.manual_halt(hours=hours, reason=reason)
    text = (
        f"🛑 *TRADING HALTED*\n"
        f"  Duration: `{hours}h`\n"
        f"  Reason: `{reason}`\n"
        f"  Use `/resume` to reactivate"
    )
    await message.answer(_md_escape(text), parse_mode="MarkdownV2")


# =============================================================================
# /resume — resume after manual halt
# =============================================================================

@router.message(Command("resume"))
async def cmd_resume(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    risk = _dependencies["risk_kernel"]
    if not risk:
        await message.answer("⚠️ Risk kernel not initialised")
        return

    if risk.manual_resume():
        await message.answer(_md_escape("✅ *Trading resumed*"), parse_mode="MarkdownV2")
    else:
        status = risk.get_status()
        await message.answer(
            _md_escape(
                f"⚠️ Cannot manually resume\n"
                f"Current halt reason: `{status['halt_reason']}`\n"
                f"Halt is automatic \\(killswitch\\) — wait for expiration\\."
            ),
            parse_mode="MarkdownV2",
        )


# =============================================================================
# /risk_status — risk kernel state
# =============================================================================

@router.message(Command("risk_status"))
async def cmd_risk_status(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    risk = _dependencies["risk_kernel"]
    if not risk:
        await message.answer("⚠️ Risk kernel not initialised")
        return

    s = risk.get_status()
    halted_icon = "🔴" if s["halted"] else "🟢"
    text = (
        f"{halted_icon} *Risk Kernel Status*\n\n"
        f"  Equity: `${s['equity']:,.2f}` "
        f"\\(starting `${s['starting_equity']:,.2f}`\\)\n"
        f"  Peak: `${s['peak_equity']:,.2f}`\n"
        f"  Total DD: `{s['total_dd_pct']:.2f}%`\n\n"
        f"  Daily PnL: `${s['daily_pnl']:+.2f}`\n"
        f"  Weekly PnL: `${s['weekly_pnl']:+.2f}`\n"
        f"  Total PnL: `${s['total_pnl']:+.2f}`\n\n"
        f"  Consecutive losses: `{s['consecutive_losses']}`\n"
        f"  Halted: `{s['halted']}`\n"
        f"  Halt reason: `{s['halt_reason']}`"
    )
    if s["halt_until_utc"]:
        text += f"\n  Halt until: `{s['halt_until_utc'][:19]}`"
    await message.answer(_md_escape(text))


# =============================================================================
# /paper_pnl — paper trading PnL
# =============================================================================

@router.message(Command("paper_pnl"))
async def cmd_paper_pnl(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    ledger = _dependencies["ledger"]
    if not ledger:
        await message.answer("⚠️ Ledger not initialised")
        return

    fa_stats = ledger.get_strategy_stats("funding_arb")
    funding_total = ledger.get_total_funding_received()

    text = (
        f"📊 *Paper Trading PnL Summary*\n\n"
        f"*Funding Arb strategy:*\n"
        f"  Total fills: `{fa_stats['total_fills']}`\n"
        f"  Notional turnover: `${fa_stats['total_notional']:,.2f}`\n"
        f"  Total fees: `${fa_stats['total_fees']:,.2f}`\n"
        f"  First trade: `{fa_stats['first_trade'][:19] if fa_stats['first_trade'] else 'none'}`\n"
        f"\n*Funding payments:*\n"
        f"  Total received: `${funding_total:+.4f}`"
    )
    await message.answer(_md_escape(text), parse_mode="MarkdownV2")


# =============================================================================
# /positions — open positions
# =============================================================================

@router.message(Command("positions"))
async def cmd_positions(message: Message):
    if not _is_authorized(message.from_user.id):
        return

    fa = _dependencies["funding_arb"]
    if not fa:
        await message.answer(_md_escape("📉 No open positions \\(strategies not loaded\\)"),
                              parse_mode="MarkdownV2")
        return

    arbs = fa.get_open_arbs()
    if not arbs:
        await message.answer(_md_escape("📉 No open positions"), parse_mode="MarkdownV2")
        return

    parts = ["💼 *Open Positions*\n"]
    for arb in arbs:
        parts.append(arb.to_telegram())
        parts.append("")
    await message.answer("\n".join(parts), parse_mode="MarkdownV2")
