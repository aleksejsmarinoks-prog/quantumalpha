"""
QA Trade Trigger — Anti-Bias Gate (Live)
==========================================

First production realization of the QA Anti-Bias Gate design spec.

The Anti-Bias Gate exists because of one universal trader failure mode:
chasing a price move AFTER it has already happened. By the time you see
"BTC +5% in 30 minutes" on the news, the squeeze has already squeezed.

Three checks (per QA spec, MEMORY entry #9):
    1. PRICED-IN: Has the target asset already moved >5% intraday in the
       expected direction? If yes → skip (FOMO).
    2. RSI OVERHEATED: Is RSI(14) >70 (or <30 for short)? If yes → DCA only.
    3. POSITION CONSISTENCY: Does this signal contradict a recent active
       signal on same asset? If yes → require explicit reversal acknowledgment.

Reuses bot.core.bybit_client.BybitClient for live klines (no duplicate
HTTP infrastructure).

Author: QuantumAlpha
Version: 0.1.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING

from ..models import AssetTrigger, Direction

if TYPE_CHECKING:
    # Avoid hard dependency — works whether BybitClient is available or not.
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AntiBiasConfig:
    """Thresholds for FOMO detection."""
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    intraday_move_pct_threshold: float = 5.0  # in trigger direction
    intraday_kline_count: int = 24            # 24 × 1h = 24h trailing
    rsi_period: int = 14
    skip_if_priced_in: bool = True
    downgrade_to_dca_if_overheated: bool = True


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class AntiBiasResult:
    passed: bool                       # True = trigger allowed as-is
    verdict: str                       # 'pass' / 'dca_only' / 'skip_priced_in' / 'skip_overbought'
    rsi: Optional[float]
    intraday_change_pct: Optional[float]
    reason: str
    suggested_size_multiplier: float = 1.0  # 0.0..1.0 — scale conviction


# ---------------------------------------------------------------------------
# RSI calculation (no external deps)
# ---------------------------------------------------------------------------

def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI on a list of close prices. None if insufficient data."""
    if len(closes) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Wilder's smoothing (initial SMA, then EMA-like)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Live price provider interface
# ---------------------------------------------------------------------------

class LivePriceProvider:
    """Adapter over bot.core.bybit_client.BybitClient.

    Designed for dependency injection — tests pass a stub, production wires
    the real BybitClient. We don't import BybitClient here to avoid a hard
    dependency while QA Trade Trigger can be tested in isolation.
    """

    async def get_klines_1h(
        self, symbol: str, count: int = 24,
    ) -> Optional[List[float]]:
        """Returns list of close prices for last `count` 1h candles. None if unavailable.

        Symbol format: 'ETHUSDT' or 'ETH/USDT' — implementation should normalize.
        """
        raise NotImplementedError


class BybitLivePriceProvider(LivePriceProvider):
    """Production adapter using the deployed BybitClient.

    Constructor takes the live BybitClient instance from the main bot,
    so we share connection/session/auth state.
    """

    def __init__(self, bybit_client):
        self._client = bybit_client

    async def get_klines_1h(
        self, symbol: str, count: int = 24,
    ) -> Optional[List[float]]:
        # Normalize 'ETH/USDT' → 'ETHUSDT' (Bybit V5 perp format)
        normalized = symbol.replace("/", "").upper()
        try:
            # bot.core.bybit_client.BybitClient.get_klines(symbol, interval, limit)
            klines = await self._client.get_klines(
                symbol=normalized, interval="60", limit=count,
            )
            if not klines:
                return None
            # Bybit V5 kline: [start, open, high, low, close, volume, turnover]
            # Newest first → reverse to chronological
            closes = [float(k[4]) for k in reversed(klines)]
            return closes
        except Exception as e:
            logger.warning("BybitLivePriceProvider.get_klines failed for %s: %s", symbol, e)
            return None


# ---------------------------------------------------------------------------
# Anti-Bias Gate
# ---------------------------------------------------------------------------

class AntiBiasGate:
    """Live FOMO detector. Asynchronous — fetches klines on demand."""

    def __init__(
        self,
        price_provider: LivePriceProvider,
        config: Optional[AntiBiasConfig] = None,
    ):
        self.price_provider = price_provider
        self.config = config or AntiBiasConfig()

    async def check_trigger(self, trigger: AssetTrigger) -> AntiBiasResult:
        """Check single AssetTrigger against live market data.

        Logic:
            - LONG: skip if intraday_change > +5% in same direction (priced in)
                    or RSI > 70 (overheated)
            - SHORT: skip if intraday_change < -5% (priced in)
                     or RSI < 30 (oversold)
        """
        closes = await self.price_provider.get_klines_1h(
            trigger.ticker, count=self.config.intraday_kline_count,
        )

        if not closes or len(closes) < self.config.rsi_period + 1:
            # Cannot evaluate — fail open (allow trigger but flag low data)
            return AntiBiasResult(
                passed=True,
                verdict="pass",
                rsi=None,
                intraday_change_pct=None,
                reason="No live price data — Anti-Bias check skipped",
                suggested_size_multiplier=1.0,
            )

        # Compute metrics
        rsi = compute_rsi(closes, period=self.config.rsi_period)
        first_close = closes[0]
        last_close = closes[-1]
        intraday_change_pct = (
            (last_close - first_close) / first_close * 100.0 if first_close > 0 else 0.0
        )

        # Direction-aware checks
        if trigger.direction == Direction.LONG:
            already_pumped = intraday_change_pct >= self.config.intraday_move_pct_threshold
            overbought = rsi is not None and rsi >= self.config.rsi_overbought
        elif trigger.direction == Direction.SHORT:
            already_dumped = intraday_change_pct <= -self.config.intraday_move_pct_threshold
            already_pumped = already_dumped  # rename for unified logic below
            overbought = rsi is not None and rsi <= self.config.rsi_oversold
        else:
            # SKIP direction — should not reach Anti-Bias Gate
            return AntiBiasResult(
                passed=False, verdict="skip_priced_in",
                rsi=rsi, intraday_change_pct=intraday_change_pct,
                reason=f"Direction={trigger.direction.value} → no trade",
                suggested_size_multiplier=0.0,
            )

        # Hard skip: priced in
        if already_pumped and self.config.skip_if_priced_in:
            return AntiBiasResult(
                passed=False,
                verdict="skip_priced_in",
                rsi=rsi,
                intraday_change_pct=intraday_change_pct,
                reason=(
                    f"Already moved {intraday_change_pct:+.2f}% in 24h "
                    f"(threshold ±{self.config.intraday_move_pct_threshold}%) — FOMO risk"
                ),
                suggested_size_multiplier=0.0,
            )

        # Soft downgrade: RSI overheated → DCA only
        if overbought and self.config.downgrade_to_dca_if_overheated:
            return AntiBiasResult(
                passed=True,
                verdict="dca_only",
                rsi=rsi,
                intraday_change_pct=intraday_change_pct,
                reason=(
                    f"RSI={rsi:.1f} ({'overbought' if trigger.direction == Direction.LONG else 'oversold'}) — "
                    f"DCA only, half size"
                ),
                suggested_size_multiplier=0.5,
            )

        # All clear
        return AntiBiasResult(
            passed=True,
            verdict="pass",
            rsi=rsi,
            intraday_change_pct=intraday_change_pct,
            reason=(
                f"OK — RSI={rsi:.1f}, intraday={intraday_change_pct:+.2f}%, "
                f"clear runway"
            ),
            suggested_size_multiplier=1.0,
        )
