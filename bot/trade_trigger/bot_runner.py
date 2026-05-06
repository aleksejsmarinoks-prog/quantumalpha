"""
QA Trade Trigger — Telegram Bot Runner (Phase 3.5 — full pipeline)
====================================================================

End-to-end runtime:
  PolymarketWatcher (every 3 min)
     ↓
  PipelineOrchestrator (5 gates: dedup → velocity → classifier → mapping
                        → corroboration → anti-bias-stub)
     ↓
  Telegram alert (Bot #2) with inline buttons [Confirm / Skip / Audit]

Runs as standalone systemd service: qa-trade-trigger.service.
Does NOT share process with main qa-bot.service.

Phase 3.5 scope:
  ✅ Polymarket source LIVE (every 3 min)
  ✅ Full pipeline with audit logging
  ✅ Telegram alerts with inline buttons
  ✅ /tt_status, /tt_help, /tt_audit, /tt_sources, /tt_recent
  ⏸️ Anti-Bias gate DISABLED (BybitClient integration → Phase 4)
  ⏸️ MT Newswires MCP source → Phase 4
  ⏸️ WhiteHouse / OFAC RSS feeds → Phase 4

ENV vars required:
  TELEGRAM_BOT_TOKEN_TT      Bot token from @BotFather
  TELEGRAM_CHAT_ID_TT        Single allowed user
  AI_LAYER_ENABLED           "true" → enable Claude L2 classifier
  AI_ADVISOR_THRESHOLD       0..1 (currently informational; reserved)
  TT_DB_PATH                 default: data/trade_trigger.db
  TT_POLL_INTERVAL_SEC       default: 180 (3 min)
  TT_MIN_SCORE               default: 7.0 (set lower if running L1-only)

Author: QuantumAlpha
Version: 0.3.5
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trade_trigger.bot")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _is_authorized(user_id: int) -> bool:
    """Single-user auth check. Mirrors main bot pattern."""
    allowed = os.getenv("TELEGRAM_CHAT_ID_TT") or os.getenv("ALLOWED_USER_ID", "0")
    try:
        return int(user_id) == int(allowed)
    except (TypeError, ValueError):
        return False


def _send_chat_id() -> int:
    """Get chat_id for proactive alert messages."""
    return int(os.getenv("TELEGRAM_CHAT_ID_TT") or os.getenv("ALLOWED_USER_ID", "0"))


# ---------------------------------------------------------------------------
# Component wiring
# ---------------------------------------------------------------------------

def _build_pipeline(db, anti_bias_gate=None):
    """Wire pipeline with appropriate config based on env.

    anti_bias_gate: optional AntiBiasGate instance. If None, gate is disabled.
    """
    from .classifier import TradeTriggerClassifier
    from .filters.velocity_tracker import VelocityTracker
    from .filters.corroboration_gate import CorroborationGate
    from .pipeline import PipelineOrchestrator, PipelineConfig

    use_l2 = os.getenv("AI_LAYER_ENABLED", "false").lower() in ("true", "1", "yes")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    min_score_env = os.getenv("TT_MIN_SCORE")

    if use_l2 and api_key:
        default_min = 7.0
        l2_active = True
    else:
        default_min = 5.0
        l2_active = False
        if use_l2 and not api_key:
            logger.warning(
                "AI_LAYER_ENABLED=true but ANTHROPIC_API_KEY not set — falling back to L1"
            )

    min_score = float(min_score_env) if min_score_env else default_min

    classifier = TradeTriggerClassifier(enable_l2=l2_active)

    pipeline = PipelineOrchestrator(
        db=db,
        classifier=classifier,
        velocity=VelocityTracker(),
        corroboration=CorroborationGate(db),
        anti_bias=anti_bias_gate,
        config=PipelineConfig(
            bucket_cap_pct=10.0,
            min_actionability_score=min_score,
            require_anti_bias=(anti_bias_gate is not None),
        ),
    )
    logger.info(
        "Pipeline configured: L2=%s, min_score=%.1f, anti_bias=%s",
        l2_active, min_score,
        "active" if anti_bias_gate else "disabled",
    )
    return pipeline


async def _try_build_anti_bias_gate():
    """Best-effort: hook up BybitClient → AntiBiasGate. Returns None on failure.

    Controlled via env: TT_ANTI_BIAS=on/off (default 'on' — try to enable).
    """
    if os.getenv("TT_ANTI_BIAS", "on").lower() in ("off", "false", "0", "no"):
        logger.info("TT_ANTI_BIAS=off — anti-bias gate explicitly disabled")
        return None

    try:
        from .core_adapters.bybit_provider import try_build_bybit_provider
        from .filters.anti_bias_check import AntiBiasGate

        provider = await try_build_bybit_provider()
        if provider is None:
            return None
        return AntiBiasGate(provider)
    except Exception as e:
        logger.warning("Anti-bias gate setup failed: %s — disabled", e)
        return None


def _build_polymarket_watcher(db):
    from .sources.polymarket import PolymarketWatcher, PolymarketClient, WatcherConfig

    interval = int(os.getenv("TT_POLL_INTERVAL_SEC", "180"))
    client = PolymarketClient()
    watcher = PolymarketWatcher(
        db=db, client=client,
        config=WatcherConfig(poll_interval_seconds=interval),
    )
    logger.info(
        "Polymarket watcher configured: %d markets, %ds interval",
        len(watcher.watchlist), interval,
    )
    return watcher


def _build_government_watchers(db):
    """Build RSS watchers if enabled (TT_RSS_ENABLED, default 'on')."""
    if os.getenv("TT_RSS_ENABLED", "on").lower() in ("off", "false", "0", "no"):
        logger.info("TT_RSS_ENABLED=off — government RSS sources disabled")
        return []
    try:
        from .sources.government import all_government_watchers
        from .sources.rss_base import RSSWatcherConfig

        interval = int(os.getenv("TT_RSS_INTERVAL_SEC", "300"))
        cfg = RSSWatcherConfig(poll_interval_seconds=interval)
        watchers = all_government_watchers(db, config=cfg)
        logger.info(
            "Government RSS watchers: %d feeds (WhiteHouse, OFAC, StateDept, Fed), %ds interval",
            len(watchers), interval,
        )
        return watchers
    except ImportError as e:
        logger.warning("feedparser not installed (%s) — RSS sources disabled", e)
        return []
    except Exception as e:
        logger.warning("RSS setup failed (%s) — disabled", e)
        return []


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    from aiogram import Bot, Dispatcher, Router, F
    from aiogram.filters import Command
    from aiogram.types import Message, CallbackQuery
    from aiogram.client.default import DefaultBotProperties
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from .db import TradeTriggerDB
    from .alerts import (
        format_alert, format_audit, format_sources, format_recent,
        build_alert_keyboard,
    )

    # ---- Config ----
    token = os.getenv("TELEGRAM_BOT_TOKEN_TT")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN_TT not set — aborting")
        sys.exit(2)

    chat_id = _send_chat_id()
    if chat_id == 0:
        logger.error("TELEGRAM_CHAT_ID_TT (or ALLOWED_USER_ID) not set — aborting")
        sys.exit(2)

    db_path = os.getenv("TT_DB_PATH", "data/trade_trigger.db")
    db = TradeTriggerDB(db_path)
    db.pulse("bot_runner", {"event": "startup", "phase": "4.0"})

    # Try to build anti-bias gate (BybitClient adapter); falls back to None
    anti_bias = await _try_build_anti_bias_gate()
    pipeline = _build_pipeline(db, anti_bias_gate=anti_bias)
    pm_watcher = _build_polymarket_watcher(db)
    rss_watchers = _build_government_watchers(db)

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=None),  # plain text — no markdown crashes
    )
    dp = Dispatcher()
    router = Router()

    # =======================================================================
    # Background job: Polymarket → Pipeline → Telegram alert
    # =======================================================================

    async def polymarket_cycle() -> None:
        try:
            events = await pm_watcher.poll_once()
            await _process_events("polymarket", events)
        except Exception as e:
            logger.exception("polymarket_cycle error: %s", e)

    async def rss_cycle() -> None:
        if not rss_watchers:
            return
        for watcher in rss_watchers:
            try:
                events = await watcher.poll_once()
                await _process_events(watcher.source_name, events)
            except Exception as e:
                logger.exception("rss_cycle error in %s: %s",
                                 watcher.source_name, e)

    async def _process_events(source_label: str, events) -> None:
        if not events:
            return
        logger.info("%s emitted %d candidate events", source_label, len(events))
        for event in events:
            decision = await pipeline.process_event(event)
            if decision.fired and decision.signal is not None:
                signal_step = next(
                    (s for s in decision.audit_trail if s.get("step") == "signal"),
                    None,
                )
                signal_id = signal_step.get("signal_id") if signal_step else 0

                text = format_alert(decision.signal, signal_id)
                keyboard = build_alert_keyboard(signal_id)
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=keyboard,
                    )
                    logger.info(
                        "Alert sent: signal_id=%d event=%s source=%s",
                        signal_id, decision.signal.event_type, source_label,
                    )
                except Exception as e:
                    logger.exception("Telegram send failed: %s", e)
            else:
                logger.debug(
                    "Event %s rejected at %s: %s",
                    event.raw_id, decision.rejection_stage,
                    decision.rejection_reason,
                )

    # =======================================================================
    # Telegram commands
    # =======================================================================

    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        ab_status = "active" if anti_bias else "disabled (BybitClient unavailable)"
        await message.answer(
            "QA Trade Trigger — early event capture bot.\n\n"
            "Pipeline gates:\n"
            "  • Velocity (event ≤ 2h old)\n"
            "  • Classifier (heuristic + Claude L2)\n"
            "  • Corroboration (≥ 2 Tier-1 sources)\n"
            "  • Mapping (event → tickers)\n"
            f"  • Anti-Bias (RSI + intraday): {ab_status}\n\n"
            "Commands: /tt_status /tt_recent /tt_sources /tt_audit /tt_help"
        )

    @router.message(Command("tt_help"))
    async def cmd_help(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        await message.answer(
            "Commands:\n"
            "/tt_status — health, stats, polling state\n"
            "/tt_recent [N] — last N triggered signals (default 10)\n"
            "/tt_sources — health of all sources\n"
            "/tt_audit <id> — full filter trail for signal #id\n"
            "/tt_calibrate [days] — performance analysis & threshold suggestions\n"
            "/tt_help — this message\n\n"
            "On alert:\n"
            "[✅ Confirm] — track for backtest performance\n"
            "[❌ Skip]    — mark rejected\n"
            "[📊 Audit]   — show full filter trail"
        )

    @router.message(Command("tt_status"))
    async def cmd_status(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        stats = db.stats()
        ab_status = "🟢 active" if anti_bias else "🔴 disabled"
        text = (
            f"QA Trade Trigger — STATUS (v0.4.0)\n"
            f"{'─' * 32}\n"
            f"Events  total/24h:  {stats['total_events']} / {stats['events_24h']}\n"
            f"Signals total/24h:  {stats['total_signals']} / {stats['signals_24h']}\n"
            f"Actionable cls:     {stats['actionable_classifications']}\n"
            f"Sources healthy:    {stats['sources_healthy']} / {stats['sources_count']}\n"
            f"\n"
            f"Anti-Bias gate: {ab_status}\n"
            f"DB: {db_path}"
        )
        await message.answer(text)

    @router.message(Command("tt_recent"))
    async def cmd_recent(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        # Parse optional N arg
        parts = (message.text or "").split()
        try:
            n = int(parts[1]) if len(parts) > 1 else 10
            n = max(1, min(n, 30))
        except ValueError:
            n = 10
        rows = db.recent_signals(limit=n)
        await message.answer(format_recent(rows))

    @router.message(Command("tt_sources"))
    async def cmd_sources(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        rows = db.all_source_health()
        await message.answer(format_sources(rows))

    @router.message(Command("tt_audit"))
    async def cmd_audit(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Usage: /tt_audit <signal_id>")
            return
        try:
            sid = int(parts[1])
        except ValueError:
            await message.answer("signal_id must be integer")
            return
        sig = db.get_signal_by_id(sid)
        if not sig:
            await message.answer(f"Signal #{sid} not found")
            return
        trail = db.get_audit_trail(sig.get("raw_id", ""))
        await message.answer(format_audit(sig, trail))

    @router.message(Command("tt_calibrate"))
    async def cmd_calibrate(message: Message) -> None:
        if not _is_authorized(message.from_user.id):
            return
        from .calibration import calibrate
        parts = (message.text or "").split()
        try:
            days = int(parts[1]) if len(parts) > 1 else 7
            days = max(1, min(days, 90))
        except ValueError:
            days = 7
        report = calibrate(db, period_days=days)
        await message.answer(report.to_text())

    # =======================================================================
    # Inline button callbacks
    # =======================================================================

    @router.callback_query(F.data.startswith("tt:confirm:"))
    async def cb_confirm(query: CallbackQuery) -> None:
        if not _is_authorized(query.from_user.id):
            await query.answer("Unauthorized", show_alert=False)
            return
        try:
            sid = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await query.answer("Invalid id")
            return
        db.update_signal_user_action(sid, "confirmed")
        await query.answer("✅ Confirmed — tracked for backtest")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    @router.callback_query(F.data.startswith("tt:skip:"))
    async def cb_skip(query: CallbackQuery) -> None:
        if not _is_authorized(query.from_user.id):
            await query.answer("Unauthorized", show_alert=False)
            return
        try:
            sid = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await query.answer("Invalid id")
            return
        db.update_signal_user_action(sid, "skipped")
        await query.answer("❌ Skipped")
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    @router.callback_query(F.data.startswith("tt:audit:"))
    async def cb_audit(query: CallbackQuery) -> None:
        if not _is_authorized(query.from_user.id):
            await query.answer("Unauthorized", show_alert=False)
            return
        try:
            sid = int(query.data.split(":", 2)[2])
        except (ValueError, IndexError):
            await query.answer("Invalid id")
            return
        sig = db.get_signal_by_id(sid)
        if not sig:
            await query.answer(f"Signal #{sid} not found")
            return
        trail = db.get_audit_trail(sig.get("raw_id", ""))
        # Send as new message (preserves the alert above with its buttons)
        await bot.send_message(chat_id=chat_id, text=format_audit(sig, trail))
        await query.answer()

    dp.include_router(router)

    # =======================================================================
    # Scheduler (background polling)
    # =======================================================================

    interval_sec = int(os.getenv("TT_POLL_INTERVAL_SEC", "180"))
    rss_interval_sec = int(os.getenv("TT_RSS_INTERVAL_SEC", "300"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        polymarket_cycle, "interval", seconds=interval_sec,
        id="polymarket_cycle", max_instances=1, coalesce=True,
    )
    if rss_watchers:
        scheduler.add_job(
            rss_cycle, "interval", seconds=rss_interval_sec,
            id="rss_cycle", max_instances=1, coalesce=True,
        )
    scheduler.add_job(
        lambda: db.pulse("bot_runner", {"event": "heartbeat"}),
        "interval", seconds=60, id="heartbeat", max_instances=1, coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started: polymarket every %ds, rss every %ds (%d feeds), heartbeat 60s",
        interval_sec, rss_interval_sec, len(rss_watchers),
    )

    # =======================================================================
    # Graceful shutdown
    # =======================================================================

    stop_event = asyncio.Event()

    def _on_signal():
        logger.info("Shutdown signal received")
        try:
            db.pulse("bot_runner", {"event": "shutdown"})
        except Exception:
            pass
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    # Send startup notification (best-effort)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "QA Trade Trigger started (v0.3.5)\n"
                f"Polling Polymarket every {interval_sec}s.\n"
                "Use /tt_status to verify."
            ),
        )
    except Exception as e:
        logger.warning("Startup notification failed: %s", e)

    logger.info("QA Trade Trigger entering polling loop — db=%s chat_id=%d",
                db_path, chat_id)
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))

    await stop_event.wait()
    polling_task.cancel()
    scheduler.shutdown(wait=False)
    try:
        await bot.session.close()
    except Exception:
        pass


def main() -> None:
    """Entry point: python -m bot.trade_trigger.bot_runner"""
    logging.basicConfig(
        level=os.getenv("TT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load .env from project root
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.info(".env loaded from %s", env_path)
        else:
            logger.warning(".env not found at %s — relying on shell env", env_path)
    except ImportError:
        logger.warning("python-dotenv not installed — relying on shell env")

    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    main()
