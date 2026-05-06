"""
QA Trade Trigger — Telegram Bot Runner (Bot #2)
================================================

Standalone bot for trade-actionable alerts. Runs as separate systemd service
(qa-trade-trigger.service) — does NOT share process with main qa-bot.service.

Why separate:
  - Different alert cadence (Bot #2 fires rarely but needs immediate UX)
  - Different rate limits (storm protection during big events)
  - Independent restart (bug in Trade Trigger doesn't kill trading bot)

Commands:
  /start       — welcome
  /tt_status   — health, source state, last events, signal stats
  /tt_help     — list commands

Callbacks (inline buttons on alerts):
  tt:confirm:<id>  — user accepted signal
  tt:skip:<id>     — user skipped signal
  tt:audit:<id>    — show full audit trail (filter checks, sources)

ENV vars required:
  TELEGRAM_BOT_TOKEN_TT
  TELEGRAM_CHAT_ID_TT (single allowed user — same pattern as main bot)

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger("trade_trigger.bot")


# ---------------------------------------------------------------------------
# Auth helper (mirrors main bot pattern)
# ---------------------------------------------------------------------------

def _is_authorized(user_id: int) -> bool:
    """Single-user auth check. Matches existing pattern from
    bot.handlers.trading_commands and bot.handlers.strategy_commands.
    """
    allowed = os.getenv("TELEGRAM_CHAT_ID_TT") or os.getenv("ALLOWED_USER_ID", "0")
    try:
        return int(user_id) == int(allowed)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    """Main async entry. Lazy-imports aiogram to avoid hard dep at module load."""
    from aiogram import Bot, Dispatcher, Router, F
    from aiogram.filters import Command
    from aiogram.types import Message, CallbackQuery
    from aiogram.client.default import DefaultBotProperties

    from .db import TradeTriggerDB

    token = os.getenv("TELEGRAM_BOT_TOKEN_TT")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN_TT not set in environment — aborting")
        sys.exit(2)

    db_path = os.getenv("TT_DB_PATH", "data/trade_trigger.db")
    db = TradeTriggerDB(db_path)
    db.pulse("bot_runner", {"event": "startup"})

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=None),  # plain text, no markdown crashes
    )
    dp = Dispatcher()
    router = Router()

    # ---- /start ----
    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        await message.answer(
            "QA Trade Trigger — early event capture bot.\n\n"
            "I monitor geopolitical and macro events 24/7 and push trade-actionable "
            "alerts when 5 filters pass: corroboration, velocity, anti-bias live, "
            "actionability ≥7/10, asset mapping.\n\n"
            "Commands: /tt_status /tt_help"
        )

    # ---- /tt_help ----
    @router.message(Command("tt_help"))
    async def cmd_help(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        await message.answer(
            "Commands:\n"
            "/tt_status — health and stats\n"
            "/tt_help — this message\n\n"
            "When alert arrives:\n"
            "[Confirm] — acknowledge, will be tracked for backtest\n"
            "[Skip]    — pass, will be marked rejected\n"
            "[Audit]   — show full filter trail and sources"
        )

    # ---- /tt_status ----
    @router.message(Command("tt_status"))
    async def cmd_status(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        stats = db.stats()
        text = (
            "QA Trade Trigger — STATUS\n"
            f"\nEvents (total / 24h):  {stats['total_events']} / {stats['events_24h']}"
            f"\nSignals (total / 24h): {stats['total_signals']} / {stats['signals_24h']}"
            f"\nActionable classifications: {stats['actionable_classifications']}"
            f"\nSources: {stats['sources_healthy']} / {stats['sources_count']} healthy"
            f"\n\nDB: {db_path}"
            f"\nVersion: 0.1.0 (Phase 2 — sources not yet connected)"
        )
        await message.answer(text)

    # ---- Callback handlers (inline buttons on alerts) ----
    @router.callback_query(F.data.startswith("tt:confirm:"))
    async def cb_confirm(query: CallbackQuery) -> None:
        if not _is_authorized(query.from_user.id):
            await query.answer("Unauthorized", show_alert=False)
            return
        signal_id = int(query.data.split(":", 2)[2])
        db.update_signal_user_action(signal_id, "confirmed")
        await query.answer("Confirmed — tracked for backtest")
        await query.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("tt:skip:"))
    async def cb_skip(query: CallbackQuery) -> None:
        if not _is_authorized(query.from_user.id):
            await query.answer("Unauthorized", show_alert=False)
            return
        signal_id = int(query.data.split(":", 2)[2])
        db.update_signal_user_action(signal_id, "skipped")
        await query.answer("Skipped")
        await query.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("tt:audit:"))
    async def cb_audit(query: CallbackQuery) -> None:
        if not _is_authorized(query.from_user.id):
            await query.answer("Unauthorized", show_alert=False)
            return
        signal_id = int(query.data.split(":", 2)[2])
        # Phase 2: stub. Phase 4 will fetch full audit trail.
        await query.answer(f"Audit for signal {signal_id} — coming in Phase 4", show_alert=True)

    dp.include_router(router)

    # Heartbeat every 60s
    async def heartbeat_loop():
        while True:
            try:
                db.pulse("bot_runner", {"event": "heartbeat"})
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)
            await asyncio.sleep(60)

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _on_signal():
        logger.info("Shutdown signal received")
        db.pulse("bot_runner", {"event": "shutdown"})
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows / non-POSIX
            pass

    logger.info("QA Trade Trigger bot starting — db=%s", db_path)
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    hb_task = asyncio.create_task(heartbeat_loop())

    await stop_event.wait()
    polling_task.cancel()
    hb_task.cancel()
    await bot.session.close()


def main() -> None:
    """Entry point for: python -m bot.trade_trigger.bot_runner"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load .env from project root if present
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.info(".env loaded from %s", env_path)
    except ImportError:
        pass

    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    main()
