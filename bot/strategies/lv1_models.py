"""
LV1 — Models, Enums, Protocols
==============================

Type definitions and Protocol classes for duck-typing existing project
interfaces (RiskKernel, PnLLedger, BybitClient, QADirective).

This module has NO runtime dependencies on bot.core — production wiring
imports the real implementations; tests inject mocks.

Author: QuantForge / QuantumAlpha
Phase: 6.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, Optional, Callable, Any, runtime_checkable


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class AegisTier(str, Enum):
    """4-tier risk hierarchy from Kimi 2.6 contribution."""
    GREEN = "GREEN"        # < 3% daily DD — full size
    YELLOW = "YELLOW"      # ≥ 3% daily DD — 0.5x size, observe-only 4h
    ORANGE = "ORANGE"      # ≥ 5% daily DD — pause 24h
    RED = "RED"            # ≥ 10% daily DD — full satellite liquidation
    BLACK = "BLACK"        # ≥ 15% daily DD — emergency, close ALL


class TradeOutcome(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


# ─────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SymbolThresholds:
    """Per-symbol calibrated parameters (from blueprint §6.4)."""
    funding_long_allowed: float
    funding_long_penalty: float
    funding_short_allowed: float
    funding_short_penalty: float

    @staticmethod
    def for_symbol(symbol: str) -> "SymbolThresholds":
        """Return symbol-specific thresholds. ETH (0.0010/0.0015), SOL (0.0015/0.0020)."""
        sym = symbol.split("/")[0].upper()
        if sym == "ETH":
            return SymbolThresholds(+0.0010, +0.0015, -0.0010, -0.0015)
        if sym == "SOL":
            return SymbolThresholds(+0.0015, +0.0020, -0.0015, -0.0020)
        # Conservative default for any auto-discovered symbol
        return SymbolThresholds(+0.0008, +0.0012, -0.0008, -0.0012)


@dataclass
class MarketState:
    """Snapshot of all market inputs needed for entry evaluation."""
    symbol: str
    price: float
    spot_price: float
    funding_rate: float                # current 8h rate (decimal, e.g. 0.0001 = 0.01%)
    seconds_to_funding: int            # seconds until next funding settlement

    # Volatility
    atr_5m: float
    atr_1h: float
    atr_median_30d: float

    # Swing levels (from 5m bars, 1h lookback)
    swing_low_5m: float
    swing_high_5m: float

    # 1m candles for sweep detection (latest closed)
    last_low_1m: float
    last_high_1m: float
    last_close_1m: float
    prev_low_1m: float
    prev_high_1m: float
    prev_close_1m: float

    # CVD (cumulative volume delta)
    cvd_15m: float                     # current 15-min rolling CVD
    cvd_15m_at_prev_low: float         # CVD value at prior swing-low timestamp
    cvd_60s: float                     # last 60-second CVD (for z-score)
    cvd_rolling_median_30m: float      # robust z-score baseline
    cvd_rolling_mad_30m: float         # robust dispersion (MAD)

    # Microstructure
    spread_bps: float
    depth_10bps_usd: float             # USD notional within 10bps of mid
    book_bid: float
    book_ask: float

    # Cross-context
    btc_1m_return: float = 0.0          # BTC last-minute return (for systemic impulse)
    btc_eth_corr_60d: float = 0.85      # rolling 60-day correlation
    oi_growth_24h_pct: float = 0.0      # open interest growth
    bvol_index: float = 0.0             # Bybit volatility index (0-100)
    dxy_intraday_pct: float = 0.0       # DXY % change session-to-now
    in_calendar_event_window: bool = False  # FOMC/CPI ±15min flag

    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class QADirective:
    """Mirrors qa_bridge.QADirective — duck-typed for tests."""
    asset: str
    direction: str = "FLAT"            # "LONG" / "SHORT" / "FLAT"
    target_weight_pct: float = 0.0
    max_position_usd: float = 0.0
    regime: str = "NEUTRAL"
    s13_active: bool = False
    vix_level: float = 20.0
    top_wrong_count: int = 0
    dxy_anomaly: bool = False
    em_stress: bool = False


@dataclass
class SweepSignal:
    """Result of entry evaluation — contains full trade plan."""
    symbol: str
    direction: Direction
    entry_zone: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    risk_per_unit: float               # |entry - stop| (price units)
    composite_score: float             # 0..1 from lv1_signals.composite_score
    conviction: float                  # signal_score normalized
    regime_multiplier: float
    funding_multiplier: float
    confidence_multiplier: float       # ChatGPT's beta-shrunk
    self_critique_penalty: float       # ≤ 0
    red_flags: tuple[str, ...] = field(default_factory=tuple)
    half_size: bool = False            # set when self-critique penalty in [-0.30, -0.15]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RollingStats:
    """Empirical p, b stats from PnLLedger (rolling window)."""
    n_trades: int = 0
    wins: int = 0
    avg_win_R: float = 0.0
    avg_loss_R: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.n_trades) if self.n_trades > 0 else 0.0

    @property
    def avg_rr(self) -> float:
        if self.avg_loss_R <= 0:
            return 0.0
        return self.avg_win_R / self.avg_loss_R


@dataclass
class OpenPosition:
    """Tracks live position state for management loop."""
    symbol: str
    signal: SweepSignal
    qty: float
    notional_usd: float
    order_id: Optional[str] = None
    tp1_filled: bool = False
    tp2_filled: bool = False
    current_stop: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# PROTOCOLS — duck-typing for project integration
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class RiskKernelProto(Protocol):
    """Subset of bot.core.risk_kernel.RiskKernel needed by LV1."""
    current_equity: float

    def is_trading_allowed(self) -> bool: ...
    def daily_drawdown_pct(self) -> float: ...
    def record_trade(self, symbol: str, pnl_usd: float, was_win: bool) -> None: ...


@runtime_checkable
class LedgerProto(Protocol):
    """Subset of bot.core.pnl_ledger.PnLLedger needed by LV1."""

    def rolling_stats(self, strategy: str, n: int = 100) -> RollingStats: ...
    def log_paper_trade(self, strategy: str, signal: SweepSignal, size_usd: float) -> None: ...
    def log_event(self, strategy: str, event_type: str, payload: Any) -> None: ...


@runtime_checkable
class BybitClientProto(Protocol):
    """Minimal Bybit client surface (REST orders)."""

    async def create_order(
        self,
        symbol: str,
        type: str,           # noqa: A002 — matches CCXT API
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[dict] = None,
    ) -> dict: ...

    async def cancel_order(self, order_id: str, symbol: str) -> dict: ...


# Provider for QA directives (duck-typed callable)
QADirectiveProvider = Callable[[str], QADirective]


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Strategy identity
STRATEGY_NAME = "liquidity_vortex_v1"

# Default symbols (BTC excluded per QA protocol)
DEFAULT_SYMBOLS: tuple[str, ...] = ("ETH/USDT:USDT", "SOL/USDT:USDT")

# Risk caps (HARD)
RISK_PCT_PER_TRADE_HARD_CAP = 0.010    # 1.0% of satellite (NOT 1.5%)
KELLY_FRACTION_HARD_CAP = 0.025        # 2.5% — Kelly-side safeguard
MIN_NOTIONAL_USD = 5.0                 # Bybit perp minimum

# Quarter-Kelly multiplier (NOT Half — see blueprint §6.3)
KELLY_QUARTER = 0.25

# Market-impact cap (5% of depth_10bps)
DEPTH_NOTIONAL_FRACTION = 0.05

# Empirical priors (until ≥30 trades observed)
PRIOR_WIN_RATE = 0.42
PRIOR_AVG_RR = 2.5
BETA_SHRINK_ALPHA = 5.0
BETA_SHRINK_BETA = 5.0
SHRINK_TARGET_N = 200                  # confidence_mult = sqrt(min(n, 200) / 200)

# Signal score threshold
SIGNAL_SCORE_THRESHOLD = 0.78

# AEGIS thresholds (daily DD %)
AEGIS_YELLOW_THRESHOLD = 0.03
AEGIS_ORANGE_THRESHOLD = 0.05
AEGIS_RED_THRESHOLD = 0.10
AEGIS_BLACK_THRESHOLD = 0.15

# Self-critique penalty thresholds
SELFCRIT_BLOCK_PENALTY = -0.30
SELFCRIT_HALVE_PENALTY = -0.15
SELFCRIT_BLOCK_COOLDOWN_MIN = 60

# Order placement
ORDER_TTL_SECONDS = 90
POSTONLY_RETRY_ATTEMPTS = 2            # blueprint §8: reduced from 3 to 2
POSTONLY_REQUOTE_MS = 200

# Volatility regime gate
ATR_RATIO_MIN = 0.7
ATR_RATIO_MAX = 2.0

# Microstructure gates
MAX_SPREAD_BPS = 5.0
DEPTH_SAFETY_MULTIPLIER = 20.0         # depth_10bps ≥ 20 × planned_notional
SECONDS_TO_FUNDING_MIN = 120

# Time-based exit
POSITION_MAX_AGE_MIN = 240
TIME_EXIT_PNL_THRESHOLD_R = 0.5

# Funding-flip emergency exit
FUNDING_FLIP_THRESHOLD = 0.0010
