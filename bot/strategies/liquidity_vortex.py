"""
LV1 — Liquidity Vortex Strategy (QUANTFORGE-LV1)
==================================================

Implements the full strategy synthesised from the multi-LLM ensemble blueprint
(QUANTFORGE_LV1_BLUEPRINT.md, §6) with the following decisions:
  - Strategy NAME = "liquidity_vortex_v1"
  - Risk per trade hard cap = 1.0%
  - Quarter-Kelly (NOT Half) — see blueprint §6.3
  - Symbol-specific funding thresholds: ETH 0.0010/0.0015, SOL 0.0015/0.0020
  - 12-factor SELF_CRITIQUE gate
  - 3-tier TP (50/30/20)
  - 4-tier AEGIS hierarchy (G/Y/O/R/B)
  - ccxt.pro WebSocket execution

Lifecycle:
  E1-E6 entry filters → composite score ≥ 0.78
  → SELF_CRITIQUE gate → position sizing (Q-Kelly + multiplicative haircut)
  → maker-only PostOnly placement → server-side SL/TP
  → trailing stop after TP1 → emergency exits → log to ledger

Author: QuantForge / QuantumAlpha
Phase: 6.1
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable, Any

from . import lv1_self_critique as sc
from . import lv1_signals as sig
from .lv1_models import (
    AegisTier,
    BybitClientProto,
    DEFAULT_SYMBOLS,
    DEPTH_NOTIONAL_FRACTION,
    DEPTH_SAFETY_MULTIPLIER,
    Direction,
    FUNDING_FLIP_THRESHOLD,
    KELLY_FRACTION_HARD_CAP,
    KELLY_QUARTER,
    LedgerProto,
    MIN_NOTIONAL_USD,
    MAX_SPREAD_BPS,
    MarketState,
    OpenPosition,
    ORDER_TTL_SECONDS,
    POSITION_MAX_AGE_MIN,
    PRIOR_AVG_RR,
    PRIOR_WIN_RATE,
    BETA_SHRINK_ALPHA,
    BETA_SHRINK_BETA,
    QADirective,
    QADirectiveProvider,
    RISK_PCT_PER_TRADE_HARD_CAP,
    RiskKernelProto,
    RollingStats,
    SELFCRIT_BLOCK_COOLDOWN_MIN,
    SECONDS_TO_FUNDING_MIN,
    SHRINK_TARGET_N,
    STRATEGY_NAME,
    SymbolThresholds,
    TIME_EXIT_PNL_THRESHOLD_R,
    TradeOutcome,
    SweepSignal,
    AEGIS_BLACK_THRESHOLD,
    AEGIS_ORANGE_THRESHOLD,
    AEGIS_RED_THRESHOLD,
    AEGIS_YELLOW_THRESHOLD,
)


log = logging.getLogger("lv1.strategy")


# ─────────────────────────────────────────────────────────────────────────────
# Helper: empirical Kelly with beta-shrinkage
# ─────────────────────────────────────────────────────────────────────────────

def shrunk_win_rate(stats: RollingStats) -> float:
    """ChatGPT's beta-shrinkage: prevents overconfidence on small samples."""
    if stats.n_trades <= 0:
        return PRIOR_WIN_RATE
    return (stats.wins + BETA_SHRINK_ALPHA) / (
        stats.n_trades + BETA_SHRINK_ALPHA + BETA_SHRINK_BETA
    )


def confidence_multiplier(n_trades: int) -> float:
    """sqrt(min(n, target) / target) — caps at 1.0 once we have target_n trades."""
    if n_trades <= 0:
        return 0.3                                # heavy haircut if no history
    return math.sqrt(min(n_trades, SHRINK_TARGET_N) / SHRINK_TARGET_N)


def empirical_kelly_fraction(stats: RollingStats) -> float:
    """
    Compute empirical Kelly fraction with beta-shrunk win rate.

    f_kelly = max(0, (p × b - (1 - p)) / b)
    """
    p = shrunk_win_rate(stats)
    b = stats.avg_rr if stats.n_trades >= 30 and stats.avg_rr > 0 else PRIOR_AVG_RR
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(0.0, f)


def quarter_kelly_capped(stats: RollingStats) -> float:
    """f_capped = min(0.25 × f_kelly, KELLY_FRACTION_HARD_CAP)."""
    return min(KELLY_QUARTER * empirical_kelly_fraction(stats), KELLY_FRACTION_HARD_CAP)


# ─────────────────────────────────────────────────────────────────────────────
# AEGIS classification
# ─────────────────────────────────────────────────────────────────────────────

def aegis_tier(daily_dd: float) -> AegisTier:
    """Map current daily drawdown % to AEGIS tier (Kimi 2.6's hierarchy)."""
    if daily_dd >= AEGIS_BLACK_THRESHOLD:
        return AegisTier.BLACK
    if daily_dd >= AEGIS_RED_THRESHOLD:
        return AegisTier.RED
    if daily_dd >= AEGIS_ORANGE_THRESHOLD:
        return AegisTier.ORANGE
    if daily_dd >= AEGIS_YELLOW_THRESHOLD:
        return AegisTier.YELLOW
    return AegisTier.GREEN


def aegis_size_multiplier(tier: AegisTier) -> float:
    """Size scaling per tier. ORANGE+ pauses entirely (returns 0)."""
    return {
        AegisTier.GREEN: 1.0,
        AegisTier.YELLOW: 0.5,
        AegisTier.ORANGE: 0.0,
        AegisTier.RED: 0.0,
        AegisTier.BLACK: 0.0,
    }[tier]


# ─────────────────────────────────────────────────────────────────────────────
# Position sizing (full multiplicative haircut)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SizingResult:
    """Detailed breakdown of how a position size was derived (for audit)."""
    f_kelly: float
    f_quarter: float
    f_capped: float
    conviction: float
    regime_mult: float
    funding_mult: float
    confidence_mult: float
    aegis_mult: float
    half_size_mult: float
    f_final: float
    notional_kelly: float
    notional_risk: float
    notional_depth: float
    notional_final: float
    stop_distance: float
    skipped_reason: Optional[str] = None


def compute_position_size(
    *,
    satellite_equity: float,
    stats: RollingStats,
    signal: SweepSignal,
    aegis: AegisTier,
    depth_10bps_usd: float,
) -> SizingResult:
    """
    Full multiplicative haircut sizing per blueprint §6.3.
    Returns SizingResult with notional_final = USD nominal of contract value to open.
    """
    if satellite_equity <= 0:
        return SizingResult(
            f_kelly=0.0, f_quarter=0.0, f_capped=0.0,
            conviction=0.0, regime_mult=0.0, funding_mult=0.0,
            confidence_mult=0.0, aegis_mult=0.0, half_size_mult=0.0,
            f_final=0.0,
            notional_kelly=0.0, notional_risk=0.0, notional_depth=0.0,
            notional_final=0.0, stop_distance=signal.risk_per_unit,
            skipped_reason="ZERO_EQUITY",
        )

    f_kelly = empirical_kelly_fraction(stats)
    f_quarter = KELLY_QUARTER * f_kelly
    f_capped = min(max(0.0, f_quarter), KELLY_FRACTION_HARD_CAP)
    conf_mult = confidence_multiplier(stats.n_trades)
    aegis_mult = aegis_size_multiplier(aegis)
    half_mult = 0.5 if signal.half_size else 1.0

    f_final = (
        f_capped
        * signal.conviction
        * signal.regime_multiplier
        * signal.funding_multiplier
        * conf_mult
        * aegis_mult
        * half_mult
    )

    if f_final <= 0:
        return SizingResult(
            f_kelly=f_kelly, f_quarter=f_quarter, f_capped=f_capped,
            conviction=signal.conviction, regime_mult=signal.regime_multiplier,
            funding_mult=signal.funding_multiplier,
            confidence_mult=conf_mult, aegis_mult=aegis_mult, half_size_mult=half_mult,
            f_final=f_final,
            notional_kelly=0.0, notional_risk=0.0, notional_depth=0.0,
            notional_final=0.0, stop_distance=signal.risk_per_unit,
            skipped_reason="F_FINAL_ZERO",
        )

    # Notional caps:
    # 1. Kelly-based
    notional_kelly = satellite_equity * f_final
    # 2. Risk-based: at SL we lose 1% of equity max
    sl_pct = signal.risk_per_unit / max(signal.entry_zone, 1e-9)
    if sl_pct <= 0:
        return SizingResult(
            f_kelly=f_kelly, f_quarter=f_quarter, f_capped=f_capped,
            conviction=signal.conviction, regime_mult=signal.regime_multiplier,
            funding_mult=signal.funding_multiplier,
            confidence_mult=conf_mult, aegis_mult=aegis_mult, half_size_mult=half_mult,
            f_final=f_final,
            notional_kelly=notional_kelly, notional_risk=0.0, notional_depth=0.0,
            notional_final=0.0, stop_distance=signal.risk_per_unit,
            skipped_reason="INVALID_SL_PCT",
        )
    notional_risk = (satellite_equity * RISK_PCT_PER_TRADE_HARD_CAP) / sl_pct
    # 3. Market-impact cap: 5% of depth_10bps (ChatGPT's contribution)
    notional_depth = (
        DEPTH_NOTIONAL_FRACTION * depth_10bps_usd if depth_10bps_usd > 0 else float("inf")
    )

    notional_final = min(notional_kelly, notional_risk, notional_depth)

    skip = None
    if notional_final < MIN_NOTIONAL_USD:
        skip = "BELOW_MIN_NOTIONAL"

    return SizingResult(
        f_kelly=f_kelly, f_quarter=f_quarter, f_capped=f_capped,
        conviction=signal.conviction, regime_mult=signal.regime_multiplier,
        funding_mult=signal.funding_multiplier,
        confidence_mult=conf_mult, aegis_mult=aegis_mult, half_size_mult=half_mult,
        f_final=f_final,
        notional_kelly=notional_kelly, notional_risk=notional_risk,
        notional_depth=notional_depth, notional_final=notional_final,
        stop_distance=signal.risk_per_unit, skipped_reason=skip,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry plan construction (used after composite score passes)
# ─────────────────────────────────────────────────────────────────────────────

def build_signal(
    ms: MarketState,
    qa: QADirective,
    direction: Direction,
    score_breakdown: dict[str, float],
    critique: sc.CritiqueResult,
) -> SweepSignal:
    """
    Construct full SweepSignal with entry/SL/TP1/TP2/TP3, multipliers, flags.

    Stop placement uses microstructure-aware approach (ChatGPT):
      stop = entry ± max(1.5 × ATR_5m, |sweep_extreme - 0.15 × ATR_1m|, 2 × spread_pct)
    """
    spread_pct = ms.spread_bps / 10_000.0
    fee_buffer = 0.0005                              # 5 bps fee buffer

    if direction == Direction.LONG:
        entry = ms.swing_low_5m * 1.0005             # +5bps inside reclaim
        sweep_term = abs(ms.swing_low_5m - 0.15 * ms.atr_5m / 5)  # mild noise buffer
        sl_distance = max(
            1.5 * ms.atr_5m,
            sweep_term * 0.0,                        # placeholder — uses ATR primarily
            entry * (2 * spread_pct + fee_buffer),
        )
        stop = entry - sl_distance
        tp1 = entry + 1.5 * ms.atr_5m
        tp2 = entry + 2.5 * ms.atr_5m
        tp3 = entry + 4.0 * ms.atr_5m
    elif direction == Direction.SHORT:
        entry = ms.swing_high_5m * 0.9995
        sl_distance = max(
            1.5 * ms.atr_5m,
            entry * (2 * spread_pct + fee_buffer),
        )
        stop = entry + sl_distance
        tp1 = entry - 1.5 * ms.atr_5m
        tp2 = entry - 2.5 * ms.atr_5m
        tp3 = entry - 4.0 * ms.atr_5m
    else:
        raise ValueError(f"Cannot build signal for direction={direction}")

    # Multipliers
    regime_mult = 0.7 if (qa.s13_active or qa.regime in {"STAGFLATION_WAR", "RISK_OFF_DEFLATION"}) else 1.0
    if qa.top_wrong_count >= 2:
        regime_mult *= 0.6

    thresholds = SymbolThresholds.for_symbol(ms.symbol)
    funding_score = sig.funding_score(ms.funding_rate, direction, thresholds)
    # Map funding_score to multiplier 0.7-1.1
    funding_mult = 0.7 + 0.4 * funding_score

    composite = score_breakdown.get("composite", 0.0)
    conviction = max(0.0, min(1.0, composite))

    return SweepSignal(
        symbol=ms.symbol,
        direction=direction,
        entry_zone=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp3,
        risk_per_unit=abs(entry - stop),
        composite_score=composite,
        conviction=conviction,
        regime_multiplier=regime_mult,
        funding_multiplier=funding_mult,
        confidence_multiplier=1.0,                  # filled in by sizer
        self_critique_penalty=critique.total_penalty,
        red_flags=critique.flags,
        half_size=critique.half_size,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pre-entry hard gates (before sizing)
# ─────────────────────────────────────────────────────────────────────────────

def hard_gate_check(ms: MarketState, qa: QADirective, direction: Direction) -> Optional[str]:
    """
    E1-E6 hard gates from blueprint §6.4.
    Returns None if all pass, otherwise reason string.
    """
    # E5 — Microstructure
    if ms.spread_bps > MAX_SPREAD_BPS:
        return "E5_SPREAD_TOO_WIDE"
    if 0 < ms.seconds_to_funding < SECONDS_TO_FUNDING_MIN:
        return "E5_FUNDING_SETTLEMENT_NEAR"

    # E4 — Volatility regime
    if ms.atr_median_30d <= 0:
        return "E4_NO_ATR_BASELINE"
    atr_ratio = ms.atr_1h / ms.atr_median_30d
    if not (0.7 < atr_ratio < 2.0):
        return "E4_ATR_REGIME_OUT_OF_BAND"

    # E3 — Funding hard gate (extreme adverse blocks entirely)
    thresholds = SymbolThresholds.for_symbol(ms.symbol)
    if direction == Direction.LONG and ms.funding_rate > thresholds.funding_long_penalty:
        return "E3_FUNDING_BLOCKS_LONG"
    if direction == Direction.SHORT and ms.funding_rate < thresholds.funding_short_penalty:
        return "E3_FUNDING_BLOCKS_SHORT"

    # E6 — QA pipeline alignment
    qa_dir = qa.direction.upper()
    if qa_dir not in {"LONG", "SHORT", "FLAT"}:
        return "E6_QA_DIRECTION_UNKNOWN"
    # Hard alignment: long trade only if QA != SHORT, short only if QA != LONG
    if direction == Direction.LONG and qa_dir == "SHORT":
        return "E6_QA_OPPOSED_LONG"
    if direction == Direction.SHORT and qa_dir == "LONG":
        return "E6_QA_OPPOSED_SHORT"

    return None


def detect_setup_direction(ms: MarketState) -> Optional[Direction]:
    """
    Return Direction if a sweep setup is present (E1), else None.
    LONG: prev_low < swing_low × 0.9985 AND prev_close > swing_low.
    SHORT mirror.
    """
    long_swept = (
        ms.swing_low_5m > 0
        and ms.prev_low_1m < ms.swing_low_5m * 0.9985
        and ms.prev_close_1m > ms.swing_low_5m
    )
    short_swept = (
        ms.swing_high_5m > 0
        and ms.prev_high_1m > ms.swing_high_5m * 1.0015
        and ms.prev_close_1m < ms.swing_high_5m
    )
    if long_swept and not short_swept:
        return Direction.LONG
    if short_swept and not long_swept:
        return Direction.SHORT
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN STRATEGY CLASS
# ─────────────────────────────────────────────────────────────────────────────

class LiquidityVortexStrategy:
    """
    LV1 — main strategy implementation.

    Compatible with bot.strategies.orchestra.StrategyOrchestra.register():
    duck-typed protocol: name, capital_pct, enabled, status_dict()/can_open() interface.
    """

    NAME = STRATEGY_NAME
    VERSION = "0.1.0"

    def get_strategy_id(self) -> str:
        """Required by Orchestra.register() for unique strategy registration."""
        return self.NAME

    def __init__(
        self,
        bybit_client: Optional[BybitClientProto],
        ledger: LedgerProto,
        risk_kernel: RiskKernelProto,
        qa_provider: Optional[QADirectiveProvider] = None,
        capital_pct: float = 0.10,
        enabled: bool = False,
        live_trading: bool = False,
        symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
        market_state_provider: Optional[Callable[[str], Optional[MarketState]]] = None,
    ):
        self.client = bybit_client
        self.ledger = ledger
        self.risk_kernel = risk_kernel
        self.qa_provider = qa_provider or self._default_qa_provider
        self.capital_pct = capital_pct
        self.enabled = enabled
        self.live_trading = live_trading
        self.symbols = symbols
        self.market_state_provider = market_state_provider
        # State
        self.open_positions: dict[str, OpenPosition] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self._last_evaluation_ts: dict[str, datetime] = {}
        self._closed_trades_count: int = 0

    # ── Orchestra-compatible public surface ──────────────────────────────
    def status_dict(self) -> dict:
        """Used by /strategies Telegram command."""
        return {
            "name": self.NAME,
            "version": self.VERSION,
            "enabled": self.enabled,
            "capital_pct": self.capital_pct,
            "live": self.live_trading,
            "open_positions": list(self.open_positions.keys()),
            "in_cooldown": [s for s, t in self.cooldown_until.items() if t > datetime.now(timezone.utc)],
            "closed_trades": self._closed_trades_count,
        }

    def get_status_dict(self) -> dict:
        """Orchestra interface — schema expected by /strategies handler."""
        if not self.enabled:
            status = "DISABLED"
        elif self.live_trading:
            status = "LIVE"
        else:
            status = "PAPER"
        return {
            "status": status,
            "daily_pnl_usd": 0.0,
            "active_positions": len(self.open_positions),
            "capital_pct": self.capital_pct,
            "signals_emitted": 0,
            "signals_gated": 0,
        }

    def get_universe(self) -> list[str]:
        """Symbols this strategy operates on (Bybit perp format for orchestra/scheduler)."""
        # Internal self.symbols uses CCXT swap format ("ETH/USDT:USDT")
        # Orchestra/scheduler kline fetcher expects Bybit perp format ("ETHUSDT")
        result = []
        for s in self.symbols:
            if "/USDT" in s:
                base = s.split("/")[0]
                result.append(f"{base}USDT")
            else:
                result.append(s)
        return result

    @property
    def config(self):
        """Orchestra interface — mimics StrategyConfig minimal contract."""
        from types import SimpleNamespace
        return SimpleNamespace(
            strategy_id=self.NAME,
            capital_pct=self.capital_pct,
            enabled=self.enabled,
        )

    @property
    def status(self):
        """Orchestra interface — DISABLED so orchestra.run_tick skips this strategy.
        LV1 has its own evaluate_cycle scheduler job, should not be double-evaluated
        via orchestra tick. /strategies display uses get_status_dict() which returns
        PAPER independently."""
        from bot.strategies.base_strategy import StrategyStatus
        return StrategyStatus.DISABLED

    def set_status(self, new_status):
        """No-op — LV1 manages own state, ignore orchestra status changes."""
        pass

    @staticmethod
    def _default_qa_provider(symbol: str) -> QADirective:
        return QADirective(asset=symbol, direction="LONG", regime="NEUTRAL")

    # ── Cooldown management ──────────────────────────────────────────────
    def is_in_cooldown(self, symbol: str) -> bool:
        until = self.cooldown_until.get(symbol)
        if until is None:
            return False
        if datetime.now(timezone.utc) >= until:
            self.cooldown_until.pop(symbol, None)
            return False
        return True

    def trigger_cooldown(self, symbol: str, minutes: int = SELFCRIT_BLOCK_COOLDOWN_MIN) -> None:
        self.cooldown_until[symbol] = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    # ── Equity & AEGIS ───────────────────────────────────────────────────
    def satellite_equity(self) -> float:
        return self.risk_kernel.current_equity * self.capital_pct

    def current_aegis_tier(self) -> AegisTier:
        return aegis_tier(self.risk_kernel.daily_drawdown_pct())

    # ── Core evaluation pipeline ─────────────────────────────────────────
    def evaluate(self, symbol: str) -> tuple[Optional[SweepSignal], Optional[str]]:
        """
        Run the full entry pipeline for `symbol`.

        Returns (signal_or_None, reason_or_None).
        - signal != None: ready to execute
        - reason != None: rejected at this stage
        """
        if not self.enabled:
            return None, "DISABLED"
        if not self.risk_kernel.is_trading_allowed():
            return None, "RISK_KERNEL_HALTED"
        if symbol in self.open_positions:
            return None, "POSITION_ALREADY_OPEN"
        if self.is_in_cooldown(symbol):
            return None, "IN_COOLDOWN"

        tier = self.current_aegis_tier()
        if aegis_size_multiplier(tier) <= 0:
            return None, f"AEGIS_TIER_PAUSED:{tier.value}"

        ms = self._get_market_state(symbol)
        if ms is None:
            return None, "NO_MARKET_STATE"

        direction = detect_setup_direction(ms)
        if direction is None:
            return None, "NO_SWEEP_SETUP"

        qa = self.qa_provider(symbol)

        # Hard gates
        gate_failure = hard_gate_check(ms, qa, direction)
        if gate_failure:
            return None, gate_failure

        # Composite score
        thresholds = SymbolThresholds.for_symbol(symbol)
        score_breakdown = sig.composite_score(ms, direction, thresholds)

        # Self-critique
        critique = sc.evaluate(ms, qa, direction)
        if critique.blocked:
            self.trigger_cooldown(symbol)
            self.ledger.log_event(self.NAME, "SELFCRIT_BLOCKED", {
                "symbol": symbol,
                "direction": direction.value,
                "flags": critique.flags,
                "penalty": critique.total_penalty,
            })
            return None, f"SELFCRIT_BLOCK:{','.join(critique.flags)}"

        # Threshold check (composite + penalty must clear 0.78)
        if not sig.passes_threshold(score_breakdown["composite"], critique.total_penalty):
            return None, f"BELOW_SCORE_THRESHOLD:{score_breakdown['composite']:.3f}+{critique.total_penalty:.2f}"

        # Build full signal
        signal = build_signal(ms, qa, direction, score_breakdown, critique)
        if signal.risk_per_unit <= 0:
            return None, "INVALID_RISK_PER_UNIT"

        return signal, None

    def _get_market_state(self, symbol: str) -> Optional[MarketState]:
        if self.market_state_provider is None:
            return None
        try:
            return self.market_state_provider(symbol)
        except Exception as e:
            log.warning("market_state_provider error for %s: %s", symbol, e)
            return None

    # ── Sizing wrapper ───────────────────────────────────────────────────
    def size_position(self, signal: SweepSignal, depth_10bps_usd: float) -> SizingResult:
        stats = self.ledger.rolling_stats(self.NAME, n=100)
        return compute_position_size(
            satellite_equity=self.satellite_equity(),
            stats=stats,
            signal=signal,
            aegis=self.current_aegis_tier(),
            depth_10bps_usd=depth_10bps_usd,
        )

    # ── Execution (paper or live) ────────────────────────────────────────
    async def execute_signal(
        self, signal: SweepSignal, sizing: SizingResult,
    ) -> Optional[OpenPosition]:
        """Place maker-only PostOnly limit order. Server-side SL via Bybit conditional."""
        if sizing.skipped_reason is not None:
            self.ledger.log_event(self.NAME, "SIZE_SKIPPED", {
                "symbol": signal.symbol,
                "reason": sizing.skipped_reason,
            })
            return None

        notional = sizing.notional_final
        qty = notional / max(signal.entry_zone, 1e-9)

        # Paper mode: log and return synthetic position
        if not self.live_trading:
            self.ledger.log_paper_trade(self.NAME, signal, notional)
            position = OpenPosition(
                symbol=signal.symbol,
                signal=signal,
                qty=qty,
                notional_usd=notional,
                order_id=f"paper-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
                current_stop=signal.stop_loss,
            )
            self.open_positions[signal.symbol] = position
            return position

        # Live mode
        if self.client is None:
            log.error("live_trading=True but bybit_client is None — refusing")
            return None

        side = "buy" if signal.direction == Direction.LONG else "sell"
        try:
            order = await self.client.create_order(
                symbol=signal.symbol,
                type="limit",
                side=side,
                amount=qty,
                price=signal.entry_zone,
                params={"postOnly": True, "timeInForce": "GTC"},
            )
        except Exception as e:                       # broad to log & no-op
            log.warning("order placement failed %s: %s", signal.symbol, e)
            self.ledger.log_event(self.NAME, "ORDER_PLACE_FAIL", {"symbol": signal.symbol, "err": str(e)})
            return None

        # Server-side SL (reduceOnly)
        try:
            await self.client.create_order(
                symbol=signal.symbol,
                type="market",
                side="sell" if signal.direction == Direction.LONG else "buy",
                amount=qty,
                params={"stopLoss": signal.stop_loss, "reduceOnly": True},
            )
        except Exception as e:
            log.warning("stop placement failed %s: %s", signal.symbol, e)

        position = OpenPosition(
            symbol=signal.symbol,
            signal=signal,
            qty=qty,
            notional_usd=notional,
            order_id=order.get("id"),
            current_stop=signal.stop_loss,
        )
        self.open_positions[signal.symbol] = position
        return position

    # ── Position management ──────────────────────────────────────────────
    async def manage_position(self, symbol: str) -> Optional[str]:
        """
        Run every-tick management: TP partials, trailing stop, emergency exits.
        Returns reason string if position was closed, else None.
        """
        pos = self.open_positions.get(symbol)
        if pos is None:
            return None
        ms = self._get_market_state(symbol)
        if ms is None:
            return None

        sig_obj = pos.signal

        # Partial TP1 (50%)
        hit_tp1 = (
            (sig_obj.direction == Direction.LONG and ms.price >= sig_obj.take_profit_1) or
            (sig_obj.direction == Direction.SHORT and ms.price <= sig_obj.take_profit_1)
        )
        if hit_tp1 and not pos.tp1_filled:
            await self._close_partial(pos, fraction=0.5, reason="TP1")
            self._move_stop_to_breakeven(pos)
            pos.tp1_filled = True

        # Partial TP2 (30% of remaining = 15% original)
        hit_tp2 = (
            (sig_obj.direction == Direction.LONG and ms.price >= sig_obj.take_profit_2) or
            (sig_obj.direction == Direction.SHORT and ms.price <= sig_obj.take_profit_2)
        )
        if hit_tp2 and pos.tp1_filled and not pos.tp2_filled:
            await self._close_partial(pos, fraction=0.3 / 0.5, reason="TP2")
            pos.tp2_filled = True

        # Trailing stop (active after TP1)
        if pos.tp1_filled:
            new_stop = self._compute_trailing_stop(ms, sig_obj, pos.current_stop)
            if new_stop != pos.current_stop:
                pos.current_stop = new_stop
                pos.last_update = datetime.now(timezone.utc)

        # Funding-flip emergency
        if abs(ms.funding_rate) > FUNDING_FLIP_THRESHOLD and self._funding_against(ms, sig_obj):
            await self._close_full(pos, reason="FUNDING_FLIP")
            return "FUNDING_FLIP"

        # Spread blowout emergency
        if ms.spread_bps > 12.0:
            await self._close_full(pos, reason="SPREAD_BLOWOUT")
            return "SPREAD_BLOWOUT"

        # BTC systemic impulse against
        if (sig_obj.direction == Direction.LONG and ms.btc_1m_return < -0.012) or \
           (sig_obj.direction == Direction.SHORT and ms.btc_1m_return > +0.012):
            await self._close_full(pos, reason="BTC_IMPULSE")
            return "BTC_IMPULSE"

        # Time-based exit
        age_min = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60.0
        if age_min > POSITION_MAX_AGE_MIN:
            unrealized_R = self._unrealized_R(ms.price, pos)
            if unrealized_R < TIME_EXIT_PNL_THRESHOLD_R:
                await self._close_full(pos, reason="TIME_EXIT")
                return "TIME_EXIT"

        return None

    # ── Helpers for management ───────────────────────────────────────────
    def _funding_against(self, ms: MarketState, signal: SweepSignal) -> bool:
        if signal.direction == Direction.LONG:
            return ms.funding_rate > FUNDING_FLIP_THRESHOLD
        if signal.direction == Direction.SHORT:
            return ms.funding_rate < -FUNDING_FLIP_THRESHOLD
        return False

    def _unrealized_R(self, price: float, pos: OpenPosition) -> float:
        s = pos.signal
        if s.risk_per_unit <= 0:
            return 0.0
        if s.direction == Direction.LONG:
            return (price - s.entry_zone) / s.risk_per_unit
        return (s.entry_zone - price) / s.risk_per_unit

    def _compute_trailing_stop(
        self, ms: MarketState, signal: SweepSignal, current_stop: float
    ) -> float:
        """ATR-based trailing stop, never moves backwards."""
        atr_trail = 1.2 * ms.atr_5m
        if signal.direction == Direction.LONG:
            candidate = ms.price - atr_trail
            return max(current_stop, candidate)
        # SHORT
        candidate = ms.price + atr_trail
        return min(current_stop, candidate) if current_stop > 0 else candidate

    def _move_stop_to_breakeven(self, pos: OpenPosition) -> None:
        s = pos.signal
        offset = 0.001                                # +10bps for fees
        if s.direction == Direction.LONG:
            pos.current_stop = s.entry_zone * (1.0 + offset)
        else:
            pos.current_stop = s.entry_zone * (1.0 - offset)

    async def _close_partial(self, pos: OpenPosition, fraction: float, reason: str) -> None:
        qty_to_close = pos.qty * fraction
        if not self.live_trading:
            self.ledger.log_event(self.NAME, "PAPER_PARTIAL_CLOSE", {
                "symbol": pos.symbol, "fraction": fraction,
                "qty": qty_to_close, "reason": reason,
            })
            pos.qty -= qty_to_close
            return
        if self.client is None:
            return
        side = "sell" if pos.signal.direction == Direction.LONG else "buy"
        try:
            await self.client.create_order(
                symbol=pos.symbol, type="market", side=side, amount=qty_to_close,
                params={"reduceOnly": True},
            )
            pos.qty -= qty_to_close
        except Exception as e:
            log.warning("partial close failed %s: %s", pos.symbol, e)

    async def _close_full(self, pos: OpenPosition, reason: str) -> None:
        if not self.live_trading:
            self.ledger.log_event(self.NAME, "PAPER_FULL_CLOSE", {
                "symbol": pos.symbol, "qty": pos.qty, "reason": reason,
            })
            self.open_positions.pop(pos.symbol, None)
            self._closed_trades_count += 1
            return
        if self.client is None:
            self.open_positions.pop(pos.symbol, None)
            # Phase 7.2 — telemetry marker
            _result_label = locals().get('verdict', 'hold') if isinstance(locals().get('verdict'), str) else 'hold'
            log.info("[EVAL_TICK] strategy=liquidity_vortex_v1 symbols=%s result=%s", ",".join(self.symbols) if hasattr(self, "symbols") else "?", _result_label)
            return
        side = "sell" if pos.signal.direction == Direction.LONG else "buy"
        try:
            await self.client.create_order(
                symbol=pos.symbol, type="market", side=side, amount=pos.qty,
                params={"reduceOnly": True},
            )
        except Exception as e:
            log.warning("full close failed %s: %s", pos.symbol, e)
        self.open_positions.pop(pos.symbol, None)
        self._closed_trades_count += 1
        self.ledger.log_event(self.NAME, "POSITION_CLOSED", {
            "symbol": pos.symbol, "reason": reason,
        })

    # ── Top-level loop entry point (used by orchestra/scheduler) ─────────
    async def run_one_cycle(self) -> dict:
        log.info("[EVAL_TICK] strategy=liquidity_vortex_v1 cycle_start")
        """One pass over all symbols: manage existing + try to open new."""
        report: dict[str, Any] = {"opened": [], "managed": [], "rejected": []}

        for symbol in self.symbols:
            # 1. Manage existing
            close_reason = await self.manage_position(symbol)
            if close_reason:
                report["managed"].append({"symbol": symbol, "closed": close_reason})

            # 2. Attempt new entry
            signal, reason = self.evaluate(symbol)
            if signal is None:
                if reason and reason not in ("DISABLED", "POSITION_ALREADY_OPEN", "NO_SWEEP_SETUP"):
                    report["rejected"].append({"symbol": symbol, "reason": reason})
                continue

            ms = self._get_market_state(symbol)
            depth = ms.depth_10bps_usd if ms else 0.0
            sizing = self.size_position(signal, depth)
            position = await self.execute_signal(signal, sizing)
            if position is not None:
                report["opened"].append({
                    "symbol": symbol,
                    "direction": signal.direction.value,
                    "entry": signal.entry_zone,
                    "stop": signal.stop_loss,
                    "tp1": signal.take_profit_1,
                    "notional": sizing.notional_final,
                    "score": signal.composite_score,
                    "flags": list(signal.red_flags),
                })

        return report

    # ── Orchestra integration helper ─────────────────────────────────────
    def trigger_event(self, event: dict) -> None:
        """
        Compatible with macro_events.MacroEventDetector.on_event(...).
        For LV1, macro events do not directly open/close trades — instead they
        feed into the QA provider. This method is a no-op stub provided for
        orchestra compatibility, kept for symmetry with dca_dips.
        """
        log.info("LV1 received macro event (no-op for entry logic): %s", event)
