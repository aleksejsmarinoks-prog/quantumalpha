"""
QuantumAlpha — main entrypoint v1.2 (commit #004 + integration fixes)

Fixes applied vs commit #004 original:
  - RiskKernel: starting_equity_usd (was: total_capital_usd)
  - PnLLedger: Path object (was: str)
  - FundingArbStrategy: (ledger, risk_kernel) (was: capital_pct, enabled)
  - FundingMonitor: (db_path=Path) (was: bybit_client=)
  - EarnManager: (ledger, bybit_client, live_mode) (was: missing ledger)
  - Trading handlers: setup_trading_commands(ledger, risk_kernel, earn_manager, funding_monitor, funding_arb)
                      + dp.include_router(trading_router)
                      (was: register_trading_handlers with wrong kwargs)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher

# Existing modules (commits #001-003)
from bot.core.bybit_client import BybitClient
from bot.core.risk_kernel import RiskKernel
from bot.core.pnl_ledger import PnLLedger
from bot.core.funding_monitor import FundingMonitor
from bot.core.earn_manager import EarnManager
from bot.handlers.trading_commands import setup_trading_commands, router as trading_router
from bot.scheduler import build_scheduler
from bot.strategies.funding_arb import FundingArbStrategy

# New modules (commit #004)
from bot.core.macro_events import MacroEventDetector, default_vix_fetcher
from bot.handlers.strategy_commands import register_strategy_handlers
from bot.strategies.cvd_divergence import CVDDivergenceStrategy
from bot.strategies.dca_dips import DCADipsStrategy
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.liquidity_vortex import LiquidityVortexStrategy
from bot.utils.market_state_provider import MarketStateProvider
from bot.strategies.orchestra import OrchestraConfig, StrategyOrchestra


# ---- logging setup ----
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

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
    logger.info("QuantumAlpha starting up — commit #004 multi-strategy (integration-fixed)")
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
    risk_kernel = RiskKernel(starting_equity_usd=total_capital)
    pnl_ledger = PnLLedger(db_path=Path("data/pnl.db"))

    # ---- 4. Funding monitor (independent service — uses public Bybit endpoint) ----
    funding_monitor = FundingMonitor(db_path=Path("data/funding.db"))

    # ---- 5. Earn manager ----
    earn_manager = EarnManager(
        ledger=pnl_ledger,
        bybit_client=bybit,
        live_mode=live_earn,
    )

    # ---- 6. Strategy instances ----
    # Funding arb (primary, commit #002/003) — uses ledger + risk_kernel API
    funding_arb = FundingArbStrategy(
        ledger=pnl_ledger,
        risk_kernel=risk_kernel,
        live_trading=not paper_mode,
    )

    # Commit #004 strategies — paper-mode capital_pct/enabled API
    mean_rev = MeanReversionStrategy(
        capital_pct=0.20,
        enabled=True,
    )
    cvd_div = CVDDivergenceStrategy(
        capital_pct=0.10,
        enabled=False,  # default disabled, enable after walk-forward validation
    )
    dca_dips = DCADipsStrategy(
        capital_pct=0.10,
        enabled=True,
    )

    # LV1 — Liquidity Vortex (Phase 6.1, dormant until market_state_provider wired)
    lv1_enabled = _env_bool("LV1_ENABLED", False)
    lv1_live = _env_bool("LV1_LIVE_TRADING", False)
    lv1_capital_pct = _env_float("LV1_CAPITAL_PCT", 0.10)

    lv1 = LiquidityVortexStrategy(
        bybit_client=bybit,
        ledger=pnl_ledger,
        risk_kernel=risk_kernel,
        qa_provider=None,
        market_state_provider=None,
        capital_pct=lv1_capital_pct,
        enabled=lv1_enabled,
        live_trading=lv1_live,
        symbols=("ETH/USDT:USDT", "SOL/USDT:USDT"),
    )

    # ──────────────────────────────────────────────────────────────────
    # Phase 6.2 — MarketStateProvider for LV1
    # Spin up only when LV1 is enabled (saves ~5MB + WS connections)
    # ──────────────────────────────────────────────────────────────────
    market_provider: MarketStateProvider | None = None
    if lv1_enabled:
        market_provider = MarketStateProvider(
            symbols=("ETH/USDT:USDT", "SOL/USDT:USDT"),
            reference_symbols=("BTC/USDT:USDT",),
            exchange_name="bybit",
            stale_threshold_sec=30,
            warmup_timeout_sec=30.0,
        )
        await market_provider.start()
        lv1.market_state_provider = market_provider
        logger.info(
            "MarketStateProvider live for LV1: %d symbols cached",
            len(market_provider._cache),
        )
    else:
        logger.info("LV1 disabled — MarketStateProvider not started")

    # ---- 7. Orchestra ----
    orchestra_config = OrchestraConfig(
        total_capital_usd=total_capital,
        paper_mode=paper_mode,
        enabled_strategies=["funding_arb_v1", "mean_reversion_v1", "dca_dips_v1", "liquidity_vortex_v1"],
        max_total_drawdown_pct=0.10,
        max_per_symbol_position_pct=0.20,
    )
    orchestra = StrategyOrchestra(orchestra_config)

    try:
        orchestra.register(funding_arb)
    except Exception as e:
        logger.warning("Could not auto-register funding_arb: %s — see adapter notes", e)
    orchestra.register(mean_rev)
    orchestra.register(cvd_div)
    orchestra.register(dca_dips)
    try:
        orchestra.register(lv1)
    except Exception as e:
        logger.warning("Could not auto-register lv1: %s — see adapter notes", e)

    logger.info("Orchestra registered with %d strategies", len(orchestra._strategies))

    # ---- 8. Macro Event Detector ----
    macro_detector = MacroEventDetector(vix_fetcher=default_vix_fetcher)
    macro_detector.on_event(dca_dips.trigger_event)
    logger.info("Macro detector wired to dca_dips")

    # ---- 9. Telegram bot ----
    bot_token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — telegram disabled")
        bot = None
        dp = None
    else:
        bot = Bot(token=bot_token)
        dp = Dispatcher()

        # Inject dependencies into trading_commands module-level _dependencies dict
        setup_trading_commands(
            ledger=pnl_ledger,
            risk_kernel=risk_kernel,
            earn_manager=earn_manager,
            funding_monitor=funding_monitor,
            funding_arb=funding_arb,
        )
        # Then attach the trading_commands router to the dispatcher
        dp.include_router(trading_router)

        # Strategy handlers (commit #004)
        register_strategy_handlers(dp, orchestra)

        logger.info("Telegram bot ready, chat_id=%s", chat_id)

    # ---- 10. Scheduler ----
    scheduler = build_scheduler(
        bybit_client=bybit,
        risk_kernel=risk_kernel,
        funding_monitor=funding_monitor,
        funding_arb=funding_arb,
        earn_manager=earn_manager,
        bot=bot,
        chat_id=chat_id,
        orchestra=orchestra,
    )

    # LV1 cycle (Phase 6.1) — dormant until market_state_provider wired (Phase 6.2)
    scheduler.add_job(
        lv1.run_one_cycle,
        trigger="interval",
        seconds=30,
        id="lv1_cycle",
        max_instances=1,
        misfire_grace_time=10,
    )

    # ---- 11. Run all background tasks ----
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
                    f"🚀 *QuantumAlpha v1.2 запущен*\n"
                    f"Mode: `{qa_mode.upper()}`\n"
                    f"Capital: `${total_capital:,.2f}`\n"
                    f"Strategies: `{len(orchestra._strategies)}`\n"
                    f"Use /strategies для статуса"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Startup notification failed: %s", e)

    # ---- 12. Graceful shutdown handling ----
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
            pass

    await stop_event.wait()

    # ---- 13. Shutdown ----
    logger.info("Shutting down…")
    try:
        macro_detector.stop()
    except Exception:
        pass
    scheduler.shutdown(wait=False)

    # Phase 6.2 — stop market provider (cancels WS tasks, closes ccxt.pro client)
    if market_provider is not None:
        try:
            await market_provider.stop()
            logger.info("MarketStateProvider stopped cleanly")
        except Exception as e:
            logger.warning("MarketStateProvider shutdown error: %s", e)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if bot:
        await bot.session.close()

    logger.info("QuantumAlpha stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)
