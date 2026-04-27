"""
bot/main.py — QuantumAlpha entry point (commit #003)

Wires together:
  - Risk Kernel
  - PnL Ledger
  - Earn Manager (read-only or live mode)
  - Funding Monitor (background async task)
  - Funding Arb Strategy (paper-mode by default)
  - Telegram bot with trading_commands router
  - APScheduler for periodic jobs

Usage:
  python -m bot.main

Environment variables (see .env.example):
  TELEGRAM_TOKEN       — bot token
  ALLOWED_USER_ID      — your Telegram user ID
  BYBIT_API_KEY        — optional, only needed for live trading or earn auto-mode
  BYBIT_API_SECRET     — optional
  BYBIT_TESTNET        — true/false (default false)
  STARTING_EQUITY_USD  — initial equity for risk kernel (default 1000)
  LIVE_TRADING         — false (paper mode is default)
  LIVE_EARN_MODE       — false (read-only earn is default)
  DATA_DIR             — where to put SQLite DBs (default ./data)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# Optional .env loading
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .core.bybit_client import BybitClient
from .core.earn_manager import EarnManager
from .core.funding_monitor import FundingMonitor, OpportunityEvent
from .core.pnl_ledger import PnLLedger
from .core.risk_kernel import RiskKernel
from .strategies.funding_arb import FundingArbStrategy
from .handlers import trading_commands
from .scheduler import build_scheduler

log = logging.getLogger("qa_bot.main")


# =============================================================================
# CONFIG
# =============================================================================

def load_config() -> dict:
    """Load + validate environment configuration."""
    cfg = {
        "telegram_token":      os.getenv("TELEGRAM_TOKEN", "").strip(),
        "allowed_user_id":     os.getenv("ALLOWED_USER_ID", "0").strip(),
        "bybit_api_key":       os.getenv("BYBIT_API_KEY", "").strip(),
        "bybit_api_secret":    os.getenv("BYBIT_API_SECRET", "").strip(),
        "bybit_testnet":       os.getenv("BYBIT_TESTNET", "false").lower() == "true",
        "starting_equity_usd": float(os.getenv("STARTING_EQUITY_USD", "1000")),
        "live_trading":        os.getenv("LIVE_TRADING", "false").lower() == "true",
        "live_earn_mode":      os.getenv("LIVE_EARN_MODE", "false").lower() == "true",
        "data_dir":            Path(os.getenv("DATA_DIR", "./data")),
        "funding_poll_sec":    int(os.getenv("FUNDING_POLL_SEC", "300")),
        "log_level":           os.getenv("LOG_LEVEL", "INFO").upper(),
    }

    if not cfg["telegram_token"]:
        log.error("TELEGRAM_TOKEN not set — cannot start bot")
        sys.exit(1)
    if cfg["allowed_user_id"] == "0":
        log.error("ALLOWED_USER_ID not set — refusing to start (security)")
        sys.exit(1)

    cfg["data_dir"].mkdir(parents=True, exist_ok=True)
    return cfg


# =============================================================================
# APPLICATION
# =============================================================================

class QuantumAlphaBot:
    """Top-level coordinator for all bot components."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

        # ── Core components ─────────────────────────────────────────────────
        self.ledger = PnLLedger(cfg["data_dir"] / "pnl.db")
        self.risk_kernel = RiskKernel(
            starting_equity_usd=cfg["starting_equity_usd"],
        )

        # ── Bybit client (optional — only needed for live mode) ─────────────
        self.bybit_client = None
        has_keys = bool(cfg["bybit_api_key"] and cfg["bybit_api_secret"])
        if has_keys:
            self.bybit_client = BybitClient(
                api_key=cfg["bybit_api_key"],
                api_secret=cfg["bybit_api_secret"],
                testnet=cfg["bybit_testnet"],
            )
            log.info(f"BybitClient created (testnet={cfg['bybit_testnet']})")
        else:
            log.warning(
                "Bybit API keys not provided — bot will run in read-only / "
                "paper-mode only (public endpoints work)"
            )

        # ── Earn manager ────────────────────────────────────────────────────
        self.earn_manager = EarnManager(
            ledger=self.ledger,
            bybit_client=self.bybit_client,
            live_mode=cfg["live_earn_mode"] and has_keys,
        )

        # ── Funding monitor ─────────────────────────────────────────────────
        self.funding_monitor = FundingMonitor(
            db_path=cfg["data_dir"] / "funding_history.db",
            poll_interval_sec=cfg["funding_poll_sec"],
            opportunity_callback=self._on_funding_opportunity,
        )

        # ── Funding arb strategy ────────────────────────────────────────────
        self.funding_arb = FundingArbStrategy(
            ledger=self.ledger,
            risk_kernel=self.risk_kernel,
            live_trading=cfg["live_trading"] and has_keys,
        )

        # ── Telegram bot ────────────────────────────────────────────────────
        self.tg_bot = Bot(
            token=cfg["telegram_token"],
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
        )
        self.dispatcher = Dispatcher()
        self._setup_dispatcher()

        # ── Scheduler ───────────────────────────────────────────────────────
        self.scheduler = build_scheduler(
            funding_monitor=self.funding_monitor,
            funding_arb=self.funding_arb,
            bybit_client=self.bybit_client,
            risk_kernel=self.risk_kernel,
            ledger=self.ledger,
        )

    def _setup_dispatcher(self):
        """Register Telegram routers and inject dependencies."""
        # Trading commands (new in commit #003)
        trading_commands.setup_trading_commands(
            ledger=self.ledger,
            risk_kernel=self.risk_kernel,
            earn_manager=self.earn_manager,
            funding_monitor=self.funding_monitor,
            funding_arb=self.funding_arb,
        )
        self.dispatcher.include_router(trading_commands.router)
        log.info("Trading commands router registered")

    async def _on_funding_opportunity(self, event: OpportunityEvent):
        """Telegram alert when high funding rate detected."""
        try:
            await self.tg_bot.send_message(
                chat_id=int(self.cfg["allowed_user_id"]),
                text=event.to_telegram(),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            log.error(f"Failed to send opportunity alert: {e}")

    # ── LIFECYCLE ──────────────────────────────────────────────────────────────

    async def start(self):
        log.info("=" * 70)
        log.info("QuantumAlpha bot starting")
        log.info(f"  data_dir       = {self.cfg['data_dir']}")
        log.info(f"  starting_eq    = ${self.cfg['starting_equity_usd']:,.2f}")
        log.info(f"  live_trading   = {self.cfg['live_trading']}")
        log.info(f"  live_earn_mode = {self.cfg['live_earn_mode']}")
        log.info(f"  bybit_testnet  = {self.cfg['bybit_testnet']}")
        log.info("=" * 70)

        # Start funding monitor background task
        await self.funding_monitor.start()

        # Start APScheduler
        self.scheduler.start()

        # Notify user via Telegram
        try:
            await self.tg_bot.send_message(
                chat_id=int(self.cfg["allowed_user_id"]),
                text=(
                    "🟢 *QuantumAlpha online*\n"
                    f"Mode: {'LIVE' if self.cfg['live_trading'] else 'PAPER'}\n"
                    f"Equity: `${self.cfg['starting_equity_usd']:,.2f}`\n"
                    "Use `/balance`, `/funding`, `/earn` to interact."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            log.warning(f"Couldn't send startup message: {e}")

        # Start Telegram polling (blocks until shutdown)
        log.info("Telegram polling started — bot is live")
        await self.dispatcher.start_polling(self.tg_bot)

    async def shutdown(self):
        log.info("Shutting down QuantumAlpha bot...")
        try:
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            log.warning(f"scheduler shutdown error: {e}")
        try:
            await self.funding_monitor.stop()
        except Exception as e:
            log.warning(f"funding_monitor stop error: {e}")
        try:
            await self.dispatcher.stop_polling()
        except Exception as e:
            log.warning(f"dispatcher stop error: {e}")
        try:
            await self.tg_bot.session.close()
        except Exception as e:
            log.warning(f"telegram session close error: {e}")
        try:
            self.ledger.close()
        except Exception as e:
            log.warning(f"ledger close error: {e}")
        log.info("Shutdown complete")


# =============================================================================
# ENTRY POINT
# =============================================================================

async def amain():
    cfg = load_config()
    logging.basicConfig(
        level=cfg["log_level"],
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = QuantumAlphaBot(cfg)

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Signal received — initiating shutdown")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows
            pass

    start_task = asyncio.create_task(app.start())
    stop_task  = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        [start_task, stop_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    await app.shutdown()


def main():
    """CLI entry. Use: `python -m bot.main`"""
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Interrupted")


if __name__ == "__main__":
    main()
