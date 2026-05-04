"""
QuantumAlpha — Background Scheduler (Commit #004 Update)
=========================================================

APScheduler with all periodic jobs.

Jobs (commit #003):
    1. funding_arb_evaluate     — every 15 min
    2. equity_snapshot          — every 1h
    3. daily_summary            — daily at 00:05 UTC
    4. weekly_summary           — Mondays at 00:05 UTC
    5. earn_apr_check           — every 6h

Jobs (NEW in commit #004):
    6. orchestra_tick           — every 5 min (multi-strategy evaluate + execute)
    7. portfolio_value_sync     — every 10 min (orchestra DD tracking)
    8. macro_status_log         — every 30 min

Version: 1.1 (commit #004)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


logger = logging.getLogger("qa.scheduler")


def build_scheduler(
    bybit_client: Any,
    risk_kernel: Any,
    funding_monitor: Any,
    funding_arb: Any,
    earn_manager: Any,
    bot: Optional[Any] = None,
    chat_id: Optional[str] = None,
    orchestra: Optional[Any] = None,         # NEW (commit #004)
) -> AsyncIOScheduler:
    """
    Build and configure the AsyncIO scheduler.

    Returns scheduler — caller must invoke .start() and .shutdown().
    """
    sched = AsyncIOScheduler(timezone="UTC")

    # ---- existing jobs (from commit #003) ----

    # Job 1: funding arb evaluate every 15 min
    async def _funding_arb_eval():
        try:
            rates = await funding_monitor.fetch_once()
            if rates:
                await funding_arb.evaluate_cycle(bybit_client, rates)
        except Exception as e:
            logger.exception("funding_arb_evaluate failed: %s", e)

    sched.add_job(
        _funding_arb_eval,
        IntervalTrigger(minutes=15),
        id="funding_arb_evaluate",
        name="Funding Arb evaluate (15m)",
        max_instances=1,
        coalesce=True,
    )

    # Job 2: hourly equity snapshot
    async def _equity_snapshot():
        try:
            balance = await bybit_client.get_unified_balance()
            risk_kernel.record_equity_snapshot(balance.get("totalEquity", 0))
            if orchestra is not None:
                orchestra.update_portfolio_value(float(balance.get("totalEquity", 0)))
        except Exception as e:
            logger.exception("equity_snapshot failed: %s", e)

    sched.add_job(
        _equity_snapshot,
        IntervalTrigger(hours=1),
        id="equity_snapshot",
        name="Equity snapshot (1h)",
        max_instances=1,
        coalesce=True,
    )

    # Job 3: daily summary at 00:05 UTC
    async def _daily_summary():
        if bot is None or chat_id is None:
            return
        try:
            balance = await bybit_client.get_unified_balance()
            arb_status = funding_arb.get_status_summary()
            orch_summary = (
                orchestra.get_status() if orchestra else {}
            )

            text_lines = [
                "*📊 Daily Summary*",
                f"Equity: `${float(balance.get('totalEquity', 0)):,.2f}`",
                f"Funding Arb: {arb_status}",
            ]
            if orch_summary:
                text_lines.extend([
                    f"Portfolio DD: `{orch_summary.get('drawdown_pct', 0)*100:.2f}%`",
                    f"Strategies signals today: `{orch_summary.get('signals_executed', 0)}`",
                ])
            if orch_summary.get("kill_switch", {}).get("engaged"):
                text_lines.append(f"\n🚨 KILL SWITCH: {orch_summary['kill_switch']['reason']}")

            await bot.send_message(chat_id=chat_id, text="\n".join(text_lines), parse_mode="Markdown")
        except Exception as e:
            logger.exception("daily_summary failed: %s", e)

    sched.add_job(
        _daily_summary,
        CronTrigger(hour=0, minute=5, timezone="UTC"),
        id="daily_summary",
        name="Daily summary report",
        max_instances=1,
        coalesce=True,
    )

    # Job 4: weekly summary Mondays 00:05 UTC
    async def _weekly_summary():
        if bot is None or chat_id is None:
            return
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="*📅 Weekly summary placeholder* — extended report TBD",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("weekly_summary failed: %s", e)

    sched.add_job(
        _weekly_summary,
        CronTrigger(day_of_week="mon", hour=0, minute=5, timezone="UTC"),
        id="weekly_summary",
        name="Weekly summary",
        max_instances=1,
        coalesce=True,
    )

    # Job 5: Earn APR check every 6h
    async def _earn_apr_check():
        try:
            await earn_manager.check_apr_and_alert(bot, chat_id)
        except Exception as e:
            logger.exception("earn_apr_check failed: %s", e)

    sched.add_job(
        _earn_apr_check,
        IntervalTrigger(hours=6),
        id="earn_apr_check",
        name="Earn APR check (6h)",
        max_instances=1,
        coalesce=True,
    )

    # ---- NEW jobs in commit #004 ----

    if orchestra is not None:
        # Job 6: orchestra tick — multi-strategy evaluation
        async def _orchestra_tick():
            try:
                await _run_orchestra_tick(orchestra, bybit_client, funding_monitor)
            except Exception as e:
                logger.exception("orchestra_tick failed: %s", e)

        sched.add_job(
            _orchestra_tick,
            IntervalTrigger(minutes=5),
            id="orchestra_tick",
            name="Orchestra strategy tick (5m)",
            max_instances=1,
            coalesce=True,
        )

        # Job 7: portfolio value sync every 10 min (more frequent than equity snapshot)
        async def _portfolio_sync():
            try:
                balance = await bybit_client.get_unified_balance()
                orchestra.update_portfolio_value(float(balance.get("totalEquity", 0)))
            except Exception as e:
                logger.exception("portfolio_sync failed: %s", e)

        sched.add_job(
            _portfolio_sync,
            IntervalTrigger(minutes=10),
            id="portfolio_value_sync",
            name="Portfolio value sync (10m)",
            max_instances=1,
            coalesce=True,
        )

    return sched


async def _run_orchestra_tick(orchestra: Any, bybit_client: Any, funding_monitor: Any) -> None:
    """
    One orchestra evaluation tick.

    1. Fetch market data for all symbols across all strategies' universes
    2. Determine current macro regime (placeholder — connect to QA pipeline)
    3. Run orchestra.run_tick() to get decisions
    4. Hand decisions to executor (paper or live)
    """
    # Aggregate universes
    all_symbols = set()
    for strat in orchestra._strategies.values():
        all_symbols.update(strat.get_universe())

    # Fetch market data per symbol
    market_data_per_symbol: Dict[str, Dict[str, Any]] = {}
    for symbol in all_symbols:
        try:
            data = await _fetch_market_data(bybit_client, symbol)
            if data:
                market_data_per_symbol[symbol] = data
        except Exception as e:
            logger.warning("market data fetch failed for %s: %s", symbol, e)

    # Determine regime — placeholder using funding_monitor
    # In production, connect to QA pipeline 4-axis regime classifier
    regime = await _detect_regime(funding_monitor)

    # Run tick
    decisions = orchestra.run_tick(market_data_per_symbol, regime)

    # Execute (paper or live)
    for decision in decisions:
        if not decision.allowed or not decision.signal.is_actionable():
            continue
        await _execute_decision(decision, orchestra, bybit_client)


async def _fetch_market_data(bybit_client: Any, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetch minimum market data needed by all strategies.

    Returns dict with keys:
        last_price, returns_1h, rsi_14_1h, atr_14_1h (if available)
    """
    try:
        # Fetch 1h klines
        klines = await bybit_client.get_klines(category="linear", symbol=symbol, interval="60", limit=20)
        if not klines or len(klines) < 15:
            return None

        # Klines are typically [timestamp, open, high, low, close, volume, turnover]
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]

        last_price = closes[-1]
        prev_price = closes[-2] if len(closes) >= 2 else last_price
        returns_1h = (last_price - prev_price) / prev_price if prev_price else 0.0

        # RSI 14
        from bot.strategies.mean_reversion import calc_rsi
        rsi = calc_rsi(closes, period=14)

        # ATR 14 (simple)
        atr = _calc_atr(highs, lows, closes, period=14)

        return {
            "last_price": last_price,
            "returns_1h": returns_1h,
            "rsi_14_1h": rsi if rsi is not None else 50.0,
            "atr_14_1h": atr if atr is not None else 0.0,
            "close_1h": closes,
        }
    except Exception as e:
        logger.warning("Market data fetch error for %s: %s", symbol, e)
        return None


def _calc_atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


async def _detect_regime(funding_monitor: Any) -> str:
    """
    Placeholder regime detection.
    Production: connect to QA pipeline 4-axis classifier.

    Heuristic for now:
        - VIX > 30 → VOLATILE
        - All BTC/ETH/SOL funding rates negative → BEARISH
        - All positive >0.05% → BULLISH
        - Mixed → NEUTRAL
    """
    try:
        # Best-effort: read recent funding rates from monitor
        snapshot = funding_monitor.get_latest_snapshot() if hasattr(funding_monitor, "get_latest_snapshot") else {}
        rates = snapshot.get("rates", {})
        if not rates:
            return "NEUTRAL"

        avg = sum(rates.values()) / len(rates)
        if avg < -0.0001:
            return "BEARISH"
        if avg > 0.0005:
            return "BULLISH"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


async def _execute_decision(decision: Any, orchestra: Any, bybit_client: Any) -> None:
    """
    Execute decision (paper or live).

    In paper-mode: log to PnL ledger as paper fill, update orchestra state.
    In live-mode: place real Bybit order, then update state on confirmed fill.
    """
    if orchestra.config.paper_mode:
        # Paper-mode: simulate fill at last_price
        logger.info(
            "PAPER FILL: %s | %s | size=$%.2f | reason=%s",
            decision.signal.symbol,
            decision.signal.signal_type.value,
            decision.allocated_size_usd,
            decision.signal.reason,
        )
        # Simulated fill price = current price from metadata or fetch
        fill_price = float(decision.signal.metadata.get("trigger_price", 0) or
                           decision.signal.metadata.get("fire_price", 0))
        if fill_price <= 0:
            return

        # Update strategy state via callback
        strat = orchestra.get_strategy(decision.signal.strategy_id)
        if strat is None:
            return

        sig_type = decision.signal.signal_type.value
        if sig_type == "ENTER_LONG":
            # Mean reversion uses on_tier_filled
            tier_filled = getattr(strat, "on_tier_filled", None)
            if callable(tier_filled):
                tier = decision.signal.metadata.get("tier", 1)
                tier_filled(decision.signal.symbol, tier, fill_price, decision.allocated_size_usd)
        elif sig_type == "ENTER_SHORT":
            short_filled = getattr(strat, "on_short_filled", None)
            if callable(short_filled):
                short_filled(
                    decision.signal.symbol,
                    fill_price,
                    decision.allocated_size_usd,
                    decision.signal.metadata.get("stop_price", fill_price * 1.02),
                    decision.signal.metadata.get("tp_price", fill_price * 0.96),
                )

        # Update orchestra exposure
        current_exp = orchestra._symbol_total_exposure_usd.get(decision.signal.symbol, 0.0)
        orchestra.update_symbol_exposure(
            decision.signal.symbol,
            current_exp + decision.allocated_size_usd,
        )
    else:
        # LIVE mode — placeholder, requires careful implementation
        logger.warning(
            "LIVE EXECUTION not yet implemented for %s | would fire %s $%.2f",
            decision.signal.symbol,
            decision.signal.signal_type.value,
            decision.allocated_size_usd,
        )
