"""
QuantumAlpha — Main Entry Point (Commit #004 Update)
=====================================================

Wires together:
- Bybit Client (from commit #001)
- Risk Kernel (from #001)
- PnL Ledger (#001)
- Funding Monitor + Funding Arb Strategy (#002)
- Earn Manager (#002)
- Telegram Bot Handlers (#002)
- Scheduler (#003)
- NEW: Orchestra coordinator (#004)
- NEW: Multi-strategy: mean_reversion + cvd_divergence + dca_dips (#004)
- NEW: Macro Event Detector (#004)

Operating modes:
    QA_MODE=paper   — paper trading only (default, safe)
    QA_MODE=live    — live trading enabled (must explicitly set)

Run:
    python -m bot.main

Environment variables (also see .env.example):
    BYBIT_API_KEY
    BYBIT_API_SECRET
    BYBIT_TESTNET=false
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    QA_MODE=paper|live
    QA_TOTAL_CAPITAL_USD=1000
    QA_LIVE_EARN_MODE=false

Version: 1.1 (commit #004)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Optional

from aiogram import Bot, Dispatcher

# Existing modules (from earlier commits)
from bot.core.bybit_client import BybitClient
from bot.core.risk_kernel import RiskKernel
from bot.core.pnl_ledger import PnLLedger
from bot.core.funding_monitor import FundingMonitor
from bot.core.earn_manager import EarnManager
from bot.handlers.trading_commands import register_trading_handlers
from bot.scheduler import build_scheduler
from bot.strategies.funding_arb import FundingArbStrategy

# New modules (commit #004)
from bot.core.macro_events import MacroEventDetector, default_vix_fetcher
from bot.handlers.strategy_commands import register_strategy_handlers
from bot.strategies.cvd_divergence import CVDDivergenceStrategy
from bot.strategies.dca_dips import DCADipsStrategy
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.orchestra import OrchestraConfig, StrategyOrchestra


# ---- logging setup ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/qa.log"),
    ],
)
logger = logging.getLogger("qa.main")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    v = _env(key, "true" if default else "false").lower()
    return v in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


async def main() -> None:
    logger.info("=" * 70)
    logger.info("QuantumAlpha starting up — commit #004 multi-strategy")
    logger.info("=" * 70)

    # ---- 1. Configuration ----
    qa_mode = _env("QA_MODE", "paper").lower()
    paper_mode = qa_mode != "live"
    total_capital = _env_float("QA_TOTAL_CAPITAL_USD", 1000.0)
    bybit_testnet = _env_bool("BYBIT_TESTNET", False)
    live_earn = _env_bool("QA_LIVE_EARN_MODE", False)

    logger.info(
        "Mode: %s | Total capital: $%.2f | Bybit testnet: %s | Live earn: %s",
        qa_mode.upper(), total_capital, bybit_testnet, live_earn,
    )

    # ---- 2. Bybit client ----
    bybit = BybitClient(
        api_key=_env("BYBIT_API_KEY"),
        api_secret=_env("BYBIT_API_SECRET"),
        testnet=bybit_testnet,
    )

    # ---- 3. Core services ----
    risk_kernel = RiskKernel(total_capital_usd=total_capital)
    pnl_ledger = PnLLedger(db_path="data/pnl.db")

    # ---- 4. Strategy instances ----
    # Funding arb (primary, written in commit #002/003)
    funding_arb = FundingArbStrategy(
        capital_pct=0.30,
        enabled=True,
    )

    # Mean reversion (commit #004) — paper-mode by default
    mean_rev = MeanReversionStrategy(
        capital_pct=0.20,
        enabled=True,
    )

    # CVD divergence (commit #004) — DEFAULT DISABLED
    # Enable only after walk-forward validation in ChronosBacktester
    cvd_div = CVDDivergenceStrategy(
        capital_pct=0.10,
        enabled=False,
    )

    # DCA dips (commit #004) — paper-mode by default; activated by macro events
    dca_dips = DCADipsStrategy(
        capital_pct=0.10,
        enabled=True,
    )

    # ---- 5. Orchestra ----
    orchestra_config = OrchestraConfig(
        total_capital_usd=total_capital,
        paper_mode=paper_mode,
        enabled_strategies=["funding_arb_v1", "mean_reversion_v1", "dca_dips_v1"],
        max_total_drawdown_pct=0.10,
        max_per_symbol_position_pct=0.20,
    )
    orchestra = StrategyOrchestra(orchestra_config)

    # Register strategies if they implement the BaseStrategy contract
    # (funding_arb_v1 from commit #002 may need adapter; documented in COMMIT_004_NOTES.md)
    try:
        orchestra.register(funding_arb)
    except Exception as e:
        logger.warning("Could not auto-register funding_arb: %s — see adapter notes", e)
    orchestra.register(mean_rev)
    orchestra.register(cvd_div)
    orchestra.register(dca_dips)

    logger.info("Orchestra registered with %d strategies", len(orchestra._strategies))

    # ---- 6. Macro Event Detector ----
    macro_detector = MacroEventDetector(vix_fetcher=default_vix_fetcher)
    macro_detector.on_event(dca_dips.trigger_event)
    logger.info("Macro detector wired to dca_dips")

    # ---- 7. Funding monitor + Earn manager (existing services) ----
    funding_monitor = FundingMonitor(bybit_client=bybit)
    earn_manager = EarnManager(bybit_client=bybit, live_mode=live_earn)

    # ---- 8. Telegram bot ----
    bot_token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — telegram disabled")
        bot = None
        dp = None
    else:
        bot = Bot(token=bot_token)
        dp = Dispatcher()

        # Existing trading handlers (from commit #002)
        register_trading_handlers(
            dispatcher=dp,
            bybit_client=bybit,
            risk_kernel=risk_kernel,
            pnl_ledger=pnl_ledger,
            funding_monitor=funding_monitor,
            earn_manager=earn_manager,
        )

        # NEW: strategy handlers (commit #004)
        register_strategy_handlers(dp, orchestra)

        logger.info("Telegram bot ready, chat_id=%s", chat_id)

    # ---- 9. Scheduler ----
    scheduler = build_scheduler(
        bybit_client=bybit,
        risk_kernel=risk_kernel,
        funding_monitor=funding_monitor,
        funding_arb=funding_arb,
        earn_manager=earn_manager,
        bot=bot,
        chat_id=chat_id,
        # NEW: pass orchestra for multi-strategy ticks
        orchestra=orchestra,
    )

    # ---- 10. Run all background tasks ----
    tasks = [
        asyncio.create_task(funding_monitor.start(), name="funding_monitor"),
        asyncio.create_task(macro_detector.start(), name="macro_detector"),
    ]

    if bot is not None and dp is not None:
        tasks.append(asyncio.create_task(dp.start_polling(bot), name="telegram_polling"))

    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Send startup notification
    if bot and chat_id:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚀 *QuantumAlpha v1.1 запущен*\n"
                    f"Mode: `{qa_mode.upper()}`\n"
                    f"Capital: `${total_capital:,.2f}`\n"
                    f"Strategies: `{len(orchestra._strategies)}`\n"
                    f"Use /strategies для статуса"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Startup notification failed: %s", e)

    # ---- 11. Graceful shutdown handling ----
    stop_event = asyncio.Event()

    def _handle_signal(signame: str):
        logger.info("Received %s — shutting down", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(signal, sig_name),
                lambda s=sig_name: _handle_signal(s),
            )
        except (NotImplementedError, ValueError):
            # Windows or non-main thread — ignore
            pass

    await stop_event.wait()

    # ---- 12. Shutdown ----
    logger.info("Shutting down…")
    macro_detector.stop()
    scheduler.shutdown(wait=False)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if bot:
        await bot.session.close()

    logger.info("QuantumAlpha stopped cleanly")


if __name__ == "__main__":
    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
