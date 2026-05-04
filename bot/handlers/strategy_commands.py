"""
QuantumAlpha — Strategy Telegram Commands
==========================================

Adds Telegram bot commands for strategy management:

    /strategies          — list all strategies + status
    /strat_status <id>   — detailed status of one strategy
    /enable_strat <id>   — enable a strategy (paper or live)
    /disable_strat <id>  — disable a strategy
    /halt_strat <id>     — emergency halt single strategy
    /resume_strat <id>   — resume halted strategy
    /strat_positions     — show all active positions across strategies
    /orchestra           — orchestra status + portfolio metrics

Module is wired into main.py via register_strategy_handlers().
Requires aiogram 3.x and a StrategyOrchestra instance.

Version: 1.0 (commit #004)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from aiogram import Router, types
from aiogram.filters import Command, CommandObject

from bot.strategies.base_strategy import StrategyStatus
from bot.strategies.orchestra import StrategyOrchestra


logger = logging.getLogger("qa.handlers.strategy")

router = Router(name="strategy_commands")


def _format_status_emoji(status: StrategyStatus) -> str:
    return {
        StrategyStatus.LIVE: "🟢",
        StrategyStatus.PAPER: "🟡",
        StrategyStatus.DISABLED: "⚫",
        StrategyStatus.HALTED: "🔴",
        StrategyStatus.COOLDOWN: "🟠",
    }.get(status, "❓")


def _format_strategy_summary(sid: str, status_dict: Dict[str, Any]) -> str:
    """One-line strategy summary."""
    status = StrategyStatus(status_dict["status"])
    emoji = _format_status_emoji(status)

    daily_pnl = status_dict.get("daily_pnl_usd", 0.0)
    pnl_str = f"+${daily_pnl:.2f}" if daily_pnl >= 0 else f"-${abs(daily_pnl):.2f}"

    return (
        f"{emoji} `{sid}`\n"
        f"   status: {status.value} | positions: {status_dict.get('active_positions', 0)} | "
        f"daily P&L: {pnl_str}\n"
        f"   capital: {status_dict.get('capital_pct', 0)*100:.0f}% | "
        f"signals: {status_dict.get('signals_emitted', 0)} emitted, "
        f"{status_dict.get('signals_gated', 0)} gated"
    )


# ---- shared orchestra reference ----
# Set by register_strategy_handlers()
_orchestra: Optional[StrategyOrchestra] = None


def register_strategy_handlers(
    dispatcher: Any,
    orchestra: StrategyOrchestra,
) -> None:
    """Wire commands into aiogram dispatcher and bind orchestra reference."""
    global _orchestra
    _orchestra = orchestra
    dispatcher.include_router(router)
    logger.info("Strategy commands registered with orchestra %s", id(orchestra))


# ---- /strategies ----
@router.message(Command("strategies"))
async def cmd_strategies(message: types.Message) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return

    status = _orchestra.get_status()
    strategies = status.get("strategies", {})

    if not strategies:
        await message.answer("No strategies registered.")
        return

    lines = ["*🎼 Стратегии:*\n"]
    for sid, sd in sorted(strategies.items()):
        lines.append(_format_strategy_summary(sid, sd))
    lines.append("")
    lines.append(f"_Orchestra paper_mode: {status['paper_mode']}_")
    lines.append(
        f"_Portfolio: ${status['portfolio_value_usd']:.2f} | "
        f"DD: {status['drawdown_pct']*100:.2f}%_"
    )

    await message.answer("\n".join(lines))


# ---- /strat_status <id> ----
@router.message(Command("strat_status"))
async def cmd_strat_status(message: types.Message, command: CommandObject) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return
    if not command.args:
        await message.answer("Usage: /strat_status <strategy_id>")
        return

    sid = command.args.strip()
    strat = _orchestra.get_strategy(sid)
    if strat is None:
        await message.answer(f"❌ Unknown strategy: `{sid}`")
        return

    status_dict = strat.get_status_dict()

    text_lines = [f"*Strategy: `{sid}`*\n"]
    text_lines.append(f"```json")
    text_lines.append(json.dumps(status_dict, indent=2, default=str)[:1500])
    text_lines.append("```")

    # Add active positions if available
    pos_method = getattr(strat, "get_position_dict", None)
    if callable(pos_method):
        positions = pos_method()
        if positions:
            text_lines.append("\n*Active positions:*")
            for sym, p in positions.items():
                text_lines.append(f"• `{sym}`: {p}")

    await message.answer("\n".join(text_lines))


# ---- /enable_strat <id> ----
@router.message(Command("enable_strat"))
async def cmd_enable_strat(message: types.Message, command: CommandObject) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return
    if not command.args:
        await message.answer("Usage: /enable_strat <strategy_id> [paper|live]")
        return

    parts = command.args.split()
    sid = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "paper"

    strat = _orchestra.get_strategy(sid)
    if strat is None:
        await message.answer(f"❌ Unknown strategy: `{sid}`")
        return

    if mode not in ("paper", "live"):
        await message.answer("❌ Mode must be 'paper' or 'live'")
        return

    # Safety: if attempting LIVE, require explicit override
    if mode == "live" and _orchestra.config.paper_mode:
        await message.answer(
            "⚠️ Cannot enable LIVE mode while orchestra is in paper_mode.\n"
            "Use /set_orchestra_live to switch the whole orchestra first."
        )
        return

    strat.config.enabled = True
    new_status = StrategyStatus.LIVE if mode == "live" else StrategyStatus.PAPER
    strat.set_status(new_status)

    await message.answer(
        f"✅ Strategy `{sid}` enabled in {mode.upper()} mode.",
    )
    logger.info("Strategy %s enabled in %s mode", sid, mode)


# ---- /disable_strat <id> ----
@router.message(Command("disable_strat"))
async def cmd_disable_strat(message: types.Message, command: CommandObject) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return
    if not command.args:
        await message.answer("Usage: /disable_strat <strategy_id>")
        return

    sid = command.args.strip()
    strat = _orchestra.get_strategy(sid)
    if strat is None:
        await message.answer(f"❌ Unknown strategy: `{sid}`")
        return

    # Safety: warn if there are open positions
    positions = getattr(strat, "_active_positions", {})
    if positions:
        await message.answer(
            f"⚠️ Strategy has {len(positions)} open position(s). "
            f"Disable still allowed but positions remain managed.\n"
            f"Use /halt_strat for emergency halt + close.",
        )

    strat.config.enabled = False
    strat.set_status(StrategyStatus.DISABLED)
    await message.answer(
        f"⚫ Strategy `{sid}` disabled.",
    )
    logger.info("Strategy %s disabled", sid)


# ---- /halt_strat <id> ----
@router.message(Command("halt_strat"))
async def cmd_halt_strat(message: types.Message, command: CommandObject) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return
    if not command.args:
        await message.answer("Usage: /halt_strat <strategy_id>")
        return

    sid = command.args.strip()
    strat = _orchestra.get_strategy(sid)
    if strat is None:
        await message.answer(f"❌ Unknown strategy: `{sid}`")
        return

    strat.set_status(StrategyStatus.HALTED)
    await message.answer(
        f"🔴 Strategy `{sid}` HALTED. Manual /resume_strat required.",
    )
    logger.warning("Strategy %s halted", sid)


# ---- /resume_strat <id> ----
@router.message(Command("resume_strat"))
async def cmd_resume_strat(message: types.Message, command: CommandObject) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return
    if not command.args:
        await message.answer("Usage: /resume_strat <strategy_id>")
        return

    sid = command.args.strip()
    strat = _orchestra.get_strategy(sid)
    if strat is None:
        await message.answer(f"❌ Unknown strategy: `{sid}`")
        return

    if strat.status not in (StrategyStatus.HALTED, StrategyStatus.COOLDOWN):
        await message.answer(
            f"⚠️ Strategy `{sid}` is in {strat.status.value} state, not HALTED/COOLDOWN.",
        )
        return

    strat.config.enabled = True
    strat.set_status(StrategyStatus.PAPER if _orchestra.config.paper_mode else StrategyStatus.LIVE)
    await message.answer(
        f"🟢 Strategy `{sid}` resumed.",
    )
    logger.info("Strategy %s resumed", sid)


# ---- /strat_positions ----
@router.message(Command("strat_positions"))
async def cmd_strat_positions(message: types.Message) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return

    lines = ["*📊 Active positions across strategies:*\n"]
    any_positions = False

    for sid, strat in _orchestra._strategies.items():
        pos_method = getattr(strat, "get_position_dict", None)
        if not callable(pos_method):
            continue
        positions = pos_method()
        if not positions:
            continue
        any_positions = True
        lines.append(f"\n*{sid}:*")
        for sym, p in positions.items():
            short = {k: v for k, v in p.items() if k in ("side", "tier", "size_usd", "age_hours")}
            lines.append(f"• `{sym}`: {short}")

    if not any_positions:
        lines.append("_No active positions._")

    await message.answer("\n".join(lines))


# ---- /orchestra ----
@router.message(Command("orchestra"))
async def cmd_orchestra(message: types.Message) -> None:
    if _orchestra is None:
        await message.answer("❌ Orchestra not initialized")
        return

    status = _orchestra.get_status()

    lines = [
        "*🎼 Orchestra status*\n",
        f"Mode: `{'PAPER' if status['paper_mode'] else 'LIVE'}`",
        f"Total capital: `${status['total_capital_usd']:,.2f}`",
        f"Portfolio value: `${status['portfolio_value_usd']:,.2f}`",
        f"Peak: `${status['portfolio_peak_usd']:,.2f}`",
        f"Drawdown: `{status['drawdown_pct']*100:.2f}%`",
        f"\nStrategies: `{len(status['strategies'])}`",
        f"Ticks processed: `{status['ticks_processed']}`",
        f"Signals: `{status['signals_evaluated']}` evaluated, `{status['signals_executed']}` executed",
    ]

    if status["kill_switch"]["engaged"]:
        lines.append(f"\n🚨 *KILL SWITCH ENGAGED*: {status['kill_switch']['reason']}")
    else:
        lines.append("\n✅ Kill switch: disengaged")

    exposures = status.get("symbol_exposures", {})
    if exposures:
        lines.append("\n*Symbol exposures:*")
        for sym, exp in exposures.items():
            lines.append(f"• `{sym}`: ${exp:.2f}")

    await message.answer("\n".join(lines))
