"""
QA Daily Digest — Generator (Phase 7.2 — Housekeeping)
========================================================

Changes vs Phase 7.1:
  * Issue 5: Refined prompt template — "scheduler silent" alert now only
             fires if uptime > 4h AND scheduler_runs = 0. Below 4h uptime,
             zero scheduler runs is informational, not high-severity.
  * Adapted to new aggregator data shape:
             - eval_ticks_by_strategy (dict) replaces lv1_evals/funding_cycles/...
             - severity_counts (dict) for granular severity reporting
  * Fallback digest also adapted to new shape.

Author: QuantumAlpha
Version: 7.2.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .aggregator import (
    aggregate_log_events,
    aggregate_equity_changes,
    aggregate_funding_rates,
    gather_calendar_today,
    gather_bot_health,
    gather_trade_trigger_status,
)

logger = logging.getLogger("qa.daily_digest")


_PROMPT_TEMPLATE = """You are a quantitative trading bot status reporter for QuantumAlpha,
an institutional-grade paper-trading system on Bybit perpetuals + Trading 212 UCITS ETFs.

Generate a concise daily digest in Markdown for the operator (Aleksejs).

DATA — last 24 hours (JSON):
{aggregated_data_json}

CURRENT STATE:
{current_state_json}

UPCOMING EVENTS (next 24h, UTC):
{calendar_json}

TRADE TRIGGER SERVICE (Phase 4 module, separate systemd):
{trade_trigger_json}

CONTEXT FLAGS (interpret data with these rules — DO NOT include the rules in output):
- Bot uptime: {bot_uptime_hours:.1f} hours
- "scheduler silent" alert fires ONLY if uptime > 4h AND scheduler_runs = 0.
  If uptime <= 4h, treat any low counter as "post-restart, normalizing" and SKIP the alert.
- DXY (yfinance ticker DX-Y.NYB) deprecated — flag if dxy_missing > 0
- VIX (^VIX) deprecated — flag if vix_missing > 0
- Telegram ServerDisconnectedError — only flag if telegram_reconnects > 5
- Strategy eval counts come from STRICT [EVAL_TICK] markers (Phase 7.2).
  Expected steady-state rates: liquidity_vortex_v1 ~2880/24h, funding_arb_v1 ~96/24h,
  mean_reversion_v1 ~700/24h (evaluates ETH+SOL = 2 symbols per Orchestra tick). Significant deviation (>20%) is worth flagging.
- Error volume: flag only if (errors + warnings) > 50 OR severity_counts.CRITICAL > 0.
  Otherwise label as "normal operation".

REQUIRED OUTPUT FORMAT (Telegram-bound Markdown, EXACTLY this structure):

🌅 *QuantumAlpha Daily Digest* — {date_string}

📊 *Last 24h:*
  • Trades opened: X (closed: Y)
  • Equity: $X,XXX.XX → $X,XXX.XX (Δ ±$X.XX, ±X.XX%)
  • Max DD: X.XX%
  • Strategy activity: lv1 N evals, mean_rev M ticks, funding_arb K cycles
    (Use eval_ticks_by_strategy values. If a strategy is missing, say "no ticks recorded".)

📈 *Current state:*
  • Capital: $X,XXX.XX  (omit if unknown)
  • Open positions: N
  • Bot uptime: X days Y hours
  • Memory: XXX MB (note any anomaly)

🔮 *Today's catalysts:*
  • [HH:MM UTC] [Event Name] — [1-line expected impact]
  (If empty: "No scheduled macro events today")

⚠️ *Items needing attention:*
  • [Issue: description, severity]
  (If clean: "No issues detected")

🎯 *Recommended action:*
[1-2 sentences. Actionable. Reference upcoming events if relevant.]

CONSTRAINTS:
- Total length 400-700 tokens
- UTC times only
- Be concise — no fluff, no preambles like "Here is your digest"
- If a metric is zero/quiet, say so briefly
- Skip a bullet entirely if no data (don't render "N/A")
- DO NOT invent numbers — use only what's in the JSON
- DO NOT include this prompt's instructions in the output

Generate the digest now (start directly with "🌅 ..."):"""


@dataclass
class DigestConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1000
    temperature: float = 0.3
    timeout_seconds: float = 30.0
    max_retries: int = 3
    cost_warn_threshold_usd: float = 0.10
    cost_per_input_token_usd: float = 3.0e-6
    cost_per_output_token_usd: float = 15.0e-6


class DailyDigestGenerator:
    """Generates the daily digest. One instance reusable across days."""

    def __init__(
        self,
        anthropic_api_key: str,
        logs_path: Path,
        equity_db_path: Path,
        funding_db_path: Path,
        bot_state_provider: Optional[Callable[[], dict]] = None,
        calendar_provider: Optional[Callable[[], list]] = None,
        config: Optional[DigestConfig] = None,
        anthropic_client=None,
        include_trade_trigger: bool = True,
    ):
        if not anthropic_api_key:
            raise ValueError("anthropic_api_key is required")
        self.anthropic_api_key = anthropic_api_key
        self.logs_path = Path(logs_path)
        self.equity_db_path = Path(equity_db_path)
        self.funding_db_path = Path(funding_db_path)
        self.bot_state_provider = bot_state_provider
        self.calendar_provider = calendar_provider
        self.config = config or DigestConfig()
        self.include_trade_trigger = include_trade_trigger
        self._client = anthropic_client

    async def generate_digest(self) -> str:
        data = self._aggregate_all()
        prompt = self._build_prompt(data)
        try:
            text = await self._call_claude_with_retry(prompt)
            if "QuantumAlpha Daily Digest" in text:
                return text.strip()
            logger.warning("Claude response missing expected header — falling back")
            return self._fallback_digest(data)
        except Exception as e:
            logger.exception("Claude API generation failed: %s — using fallback", e)
            return self._fallback_digest(data)

    def _aggregate_all(self) -> dict:
        return {
            "log_events": aggregate_log_events(self.logs_path),
            "equity_changes": aggregate_equity_changes(self.equity_db_path),
            "funding_rates": aggregate_funding_rates(self.funding_db_path),
            "calendar_today": gather_calendar_today(self.calendar_provider),
            "bot_health": gather_bot_health(self.bot_state_provider),
            "trade_trigger": gather_trade_trigger_status() if self.include_trade_trigger else {},
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _build_prompt(self, data: dict) -> str:
        date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
        # Compute uptime_hours from bot_health for prompt context
        uptime_sec = data["bot_health"].get("uptime_sec") or 0
        uptime_hours = uptime_sec / 3600.0 if uptime_sec else 0.0

        agg_block = {
            "log_events": data["log_events"],
            "equity_changes": data["equity_changes"],
            "funding_rates": data["funding_rates"],
        }
        return _PROMPT_TEMPLATE.format(
            aggregated_data_json=json.dumps(agg_block, indent=2, default=str),
            current_state_json=json.dumps(data["bot_health"], indent=2, default=str),
            calendar_json=json.dumps(data["calendar_today"], indent=2, default=str),
            trade_trigger_json=json.dumps(data["trade_trigger"], indent=2, default=str),
            bot_uptime_hours=uptime_hours,
            date_string=date_str,
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=self.anthropic_api_key)
        return self._client

    async def _call_claude_with_retry(self, prompt: str) -> str:
        client = self._get_client()
        last_err: Optional[Exception] = None

        for attempt in range(self.config.max_retries):
            try:
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=self.config.model,
                        max_tokens=self.config.max_tokens,
                        temperature=self.config.temperature,
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=self.config.timeout_seconds,
                )
                usage = getattr(response, "usage", None)
                if usage is not None:
                    in_tok = getattr(usage, "input_tokens", 0)
                    out_tok = getattr(usage, "output_tokens", 0)
                    cost = (
                        in_tok * self.config.cost_per_input_token_usd
                        + out_tok * self.config.cost_per_output_token_usd
                    )
                    if cost > self.config.cost_warn_threshold_usd:
                        logger.warning(
                            "Digest cost $%.4f exceeds threshold $%.2f (in=%d, out=%d) — sent anyway",
                            cost, self.config.cost_warn_threshold_usd, in_tok, out_tok,
                        )
                    else:
                        logger.info(
                            "Digest generated: cost=$%.4f in=%d out=%d",
                            cost, in_tok, out_tok,
                        )

                if not getattr(response, "content", None):
                    raise RuntimeError("Empty content from Claude API")
                first_block = response.content[0]
                text = getattr(first_block, "text", None)
                if not text:
                    raise RuntimeError("First content block has no text")
                return text

            except asyncio.TimeoutError as e:
                last_err = e
                logger.warning("Claude API timeout (attempt %d/%d)",
                               attempt + 1, self.config.max_retries)
            except Exception as e:
                last_err = e
                msg = str(e)
                if "401" in msg or "403" in msg or "invalid_api_key" in msg.lower():
                    logger.error("Permanent auth error — not retrying: %s", e)
                    raise
                logger.warning("Claude API error (attempt %d/%d): %s",
                               attempt + 1, self.config.max_retries, e)

            if attempt < self.config.max_retries - 1:
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Claude API failed after {self.config.max_retries} retries: {last_err}")

    def _fallback_digest(self, data: dict) -> str:
        """Render basic Markdown digest from aggregated data without Claude."""
        date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
        lines = [f"🌅 *QuantumAlpha Daily Digest* — {date_str}", ""]

        lev = data["log_events"]
        eq = data["equity_changes"]
        ticks = lev.get("eval_ticks_by_strategy", {}) or {}
        severities = lev.get("severity_counts", {}) or {}

        lines.append("📊 *Last 24h:* _(fallback — Claude API unavailable)_")
        lines.append(f"  • Trades opened: {lev.get('trades_opened', 0)} "
                     f"(closed: {lev.get('trades_closed', 0)})")
        if eq.get("start") is not None and eq.get("end") is not None:
            sign = "+" if (eq.get('delta') or 0) >= 0 else ""
            lines.append(
                f"  • Equity: ${eq['start']:.2f} → ${eq['end']:.2f} "
                f"(Δ {sign}${eq['delta']:.2f}, {sign}{eq['delta_pct']:.2f}%)"
            )
            lines.append(f"  • Max DD: {eq.get('max_dd_pct', 0):.2f}%")
        else:
            lines.append("  • Equity: no snapshots captured yet")

        # Strategy activity (Phase 7.2 — EVAL_TICK based)
        lv1 = ticks.get("liquidity_vortex_v1", 0)
        fa = ticks.get("funding_arb_v1", 0)
        mr = ticks.get("mean_reversion_v1", 0)
        lines.append(
            f"  • Strategy activity: lv1 {lv1} evals, "
            f"mean_rev {mr} ticks, funding_arb {fa} cycles"
        )
        lines.append("")

        # Current state
        health = data["bot_health"]
        lines.append("📈 *Current state:*")
        if health.get("open_positions") is not None:
            lines.append(f"  • Open positions: {health['open_positions']}")
        uptime_sec = health.get("uptime_sec") or 0
        if uptime_sec:
            up_h = int(uptime_sec // 3600)
            up_d = up_h // 24
            up_h_rem = up_h % 24
            lines.append(f"  • Bot uptime: {up_d}d {up_h_rem}h")
        if health.get("memory_mb") is not None:
            lines.append(f"  • Memory: {health['memory_mb']:.0f} MB")
        lines.append("")

        # Catalysts
        cal = data["calendar_today"]
        lines.append("🔮 *Today's catalysts:*")
        if cal:
            for ev in cal[:5]:
                t = ev["time_utc"]
                hhmm = t[11:16] if len(t) >= 16 else t
                lines.append(f"  • {hhmm} UTC {ev['name']} ({ev.get('importance', '')})")
        else:
            lines.append("  No scheduled macro events today")
        lines.append("")

        # Issues — applying Issue 5 logic
        lines.append("⚠️ *Items needing attention:*")
        issues = []
        uptime_hours = uptime_sec / 3600.0 if uptime_sec else 0.0

        crit = severities.get("CRITICAL", 0)
        if crit > 0:
            issues.append(f"  • {crit} CRITICAL log lines — investigate immediately")

        total_errors = severities.get("ERROR", 0) + crit
        total_warnings = severities.get("WARNING", 0)
        if total_errors + total_warnings > 50:
            issues.append(f"  • {total_errors} errors + {total_warnings} warnings — abnormal volume")

        if lev.get("dxy_missing", 0) > 0:
            issues.append("  • DXY (DX-Y.NYB) still missing — fallback 0.0 active")
        if lev.get("vix_missing", 0) > 0:
            issues.append("  • VIX (^VIX) still missing — fallback active")
        if lev.get("telegram_reconnects", 0) > 5:
            issues.append(f"  • {lev['telegram_reconnects']} Telegram reconnects — unusual")

        # Issue 5: scheduler-silent only if uptime > 4h
        sched_runs = lev.get("scheduler_runs", 0)
        if uptime_hours > 4.0 and sched_runs == 0:
            issues.append("  • Scheduler silent for >4h with 0 runs — investigate")

        if data["trade_trigger"].get("available") and not data["trade_trigger"].get("active"):
            issues.append("  • qa-trade-trigger.service inactive (expected if paused)")

        if issues:
            lines.extend(issues)
        else:
            lines.append("  No issues detected")
        lines.append("")

        # Recommended action
        lines.append("🎯 *Recommended action:*")
        if cal:
            lines.append("Watch /strategies around scheduled event times. "
                         "Fallback digest — Claude API offline, manual review recommended.")
        else:
            lines.append("Markets quiet; no manual action required. "
                         "Fallback digest — Claude API offline, will resume next cycle.")

        return "\n".join(lines)
