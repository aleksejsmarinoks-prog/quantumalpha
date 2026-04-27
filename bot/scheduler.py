"""
bot/scheduler.py — APScheduler with all periodic jobs (commit #003)

Jobs:
  - funding_arb_evaluate     every 15 min  → check open/close decisions
  - equity_snapshot          every 1 hour  → record equity for tracking
  - daily_summary            00:35 UTC     → Telegram daily summary
  - weekly_summary           Sunday 23:00  → Telegram weekly summary
  - earn_apr_check           every 6 hours → alert if Tier-1 APR drops
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("qa_bot.scheduler")


def build_scheduler(
    funding_monitor,
    funding_arb,
    bybit_client,
    risk_kernel,
    ledger,
) -> AsyncIOScheduler:
    """
    Build the APScheduler with all QA jobs configured.

    Returns scheduler — caller must call .start().
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    # ── 1. Funding arb evaluation cycle ─────────────────────────────────────
    async def job_funding_arb_evaluate():
        try:
            # Get latest rates from monitor's storage (no extra API call)
            latest = funding_monitor.get_latest_rates()
            if not latest:
                log.debug("funding_arb_evaluate: no rates available yet")
                return

            # Convert to FundingRate-like objects
            from .core.bybit_client import FundingRate
            import time
            rates = [
                FundingRate(
                    symbol=r["symbol"],
                    funding_rate=r["funding_rate"],
                    next_funding_time_ms=0,
                    fetched_at_utc=time.time(),
                )
                for r in latest
            ]

            if bybit_client is None:
                log.debug("funding_arb_evaluate: no bybit client, skipping")
                return

            # Use bybit_client directly (in async context)
            async with bybit_client as client:
                await funding_arb.evaluate_cycle(client, rates)
        except Exception as e:
            log.error(f"funding_arb_evaluate job error: {e}", exc_info=True)

    scheduler.add_job(
        job_funding_arb_evaluate,
        trigger=IntervalTrigger(minutes=15),
        id="funding_arb_evaluate",
        name="Funding arb open/close evaluation",
        max_instances=1,
        coalesce=True,
    )

    # ── 2. Hourly equity snapshot ───────────────────────────────────────────
    async def job_equity_snapshot():
        try:
            status = risk_kernel.get_status()
            ledger.record_equity_snapshot(
                equity_usd=status["equity"],
                source="hourly",
                metadata={
                    "daily_pnl":        status["daily_pnl"],
                    "weekly_pnl":       status["weekly_pnl"],
                    "total_pnl":        status["total_pnl"],
                    "total_dd_pct":     status["total_dd_pct"],
                    "halted":           status["halted"],
                },
            )
            log.debug(
                f"Equity snapshot: ${status['equity']:,.2f} "
                f"(DD={status['total_dd_pct']:.2f}%)"
            )
        except Exception as e:
            log.error(f"equity_snapshot job error: {e}")

    scheduler.add_job(
        job_equity_snapshot,
        trigger=IntervalTrigger(hours=1),
        id="equity_snapshot",
        name="Hourly equity snapshot",
        max_instances=1,
    )

    # ── 3. Daily summary at 00:35 UTC ───────────────────────────────────────
    async def job_daily_summary():
        # This is a stub — full implementation requires Telegram bot ref.
        # The Telegram dispatch happens via funding_monitor.opportunity_callback
        # for now; rich daily report TBD in commit #004.
        log.info("Daily summary job triggered (stub — implement in #004)")

    scheduler.add_job(
        job_daily_summary,
        trigger=CronTrigger(hour=0, minute=35, timezone="UTC"),
        id="daily_summary",
        name="Daily PnL + Earn summary",
    )

    # ── 4. Weekly summary Sunday 23:00 UTC ──────────────────────────────────
    async def job_weekly_summary():
        log.info("Weekly summary job triggered (stub — implement in #004)")

    scheduler.add_job(
        job_weekly_summary,
        trigger=CronTrigger(day_of_week="sun", hour=23, minute=0, timezone="UTC"),
        id="weekly_summary",
        name="Weekly performance summary",
    )

    # ── 5. Earn APR check every 6 hours ─────────────────────────────────────
    async def job_earn_apr_check():
        # Logs Tier-1 APR for monitoring.
        # Per DeepSeek Task #10, Tier-1 12% is promo; could change.
        try:
            if bybit_client is None:
                return
            async with bybit_client as client:
                products = await client.list_earn_products(
                    category="FlexibleSaving", coin="USDT"
                )
                for p in products:
                    apr_str = p.get("estimateApr", "0").rstrip("%")
                    try:
                        apr = float(apr_str) / 100
                    except ValueError:
                        continue
                    log.info(
                        f"Earn APR check: USDT Flexible APR = {apr*100:.2f}% "
                        f"(productId={p.get('productId')})"
                    )
        except Exception as e:
            log.error(f"earn_apr_check job error: {e}")

    scheduler.add_job(
        job_earn_apr_check,
        trigger=IntervalTrigger(hours=6),
        id="earn_apr_check",
        name="Earn APR monitoring",
    )

    log.info(
        f"Scheduler built with {len(scheduler.get_jobs())} jobs: "
        + ", ".join(j.id for j in scheduler.get_jobs())
    )
    return scheduler
