"""
QA Trade Trigger — Bybit Client Adapter
=========================================

Lazy adapter that connects PipelineOrchestrator's anti-bias gate to the
existing bot.core.bybit_client.BybitClient deployed in the main trading bot.

Design constraints:
  1. Cannot import bot.core.bybit_client at module-load time —
     would create circular dep / break tests run in isolation.
  2. BybitClient API on VPS may differ from this module's assumptions.
     Build is verified at runtime, not import-time.
  3. If anything goes wrong (missing class, missing method, init error),
     adapter returns None and pipeline runs WITHOUT anti-bias gate.
     Better degraded than crashed.

Usage in bot_runner.py:

    from .core_adapters.bybit_provider import try_build_bybit_provider

    provider = await try_build_bybit_provider()
    if provider is not None:
        anti_bias = AntiBiasGate(provider)
    else:
        anti_bias = None  # falls back to no-anti-bias mode

Author: QuantumAlpha
Version: 0.4.0
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnostic types
# ---------------------------------------------------------------------------

class BybitProviderUnavailable(RuntimeError):
    """Raised when adapter cannot be constructed. Caller should fall back."""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class BybitProvider:
    """Adapter implementing LivePriceProvider over real BybitClient.

    Compatible with multiple BybitClient API shapes:
      - get_klines(symbol, interval, limit)
      - get_klines(symbol=..., interval=..., limit=...)
      - klines() / fetch_klines() / get_kline()  (best-effort fallback)

    If none of the methods produces usable data, returns None (fail-open).
    """

    KLINE_METHOD_CANDIDATES = (
        "get_klines",
        "get_kline",
        "fetch_klines",
        "klines",
    )

    def __init__(self, client):
        self._client = client
        self._method_name: Optional[str] = None  # cached after first success

    async def get_klines_1h(
        self, symbol: str, count: int = 24,
    ) -> Optional[List[float]]:
        """Returns list of close prices, oldest first. None on any failure."""
        normalized = symbol.replace("/", "").upper()

        # Try cached method first
        if self._method_name is not None:
            closes = await self._invoke(self._method_name, normalized, count)
            if closes is not None:
                return closes
            # Cached method failed — fall through to retry detection
            self._method_name = None

        # Try each candidate method
        for name in self.KLINE_METHOD_CANDIDATES:
            if not hasattr(self._client, name):
                continue
            closes = await self._invoke(name, normalized, count)
            if closes is not None:
                self._method_name = name  # cache
                logger.info("BybitProvider: using %s for klines", name)
                return closes

        logger.warning(
            "BybitProvider: no compatible klines method on %s — anti-bias check fails open",
            type(self._client).__name__,
        )
        return None

    async def _invoke(self, method_name: str, symbol: str, count: int) -> Optional[List[float]]:
        """Try one method with multiple signature variations."""
        method = getattr(self._client, method_name, None)
        if method is None:
            return None

        # Variant 1: positional (symbol, interval, limit)
        try:
            result = method(symbol, "60", count)
            if hasattr(result, "__await__"):
                result = await result
            closes = self._extract_closes(result)
            if closes:
                return closes
        except Exception as e:
            logger.debug("%s positional failed: %s", method_name, e)

        # Variant 2: kwargs interval="1h"
        try:
            result = method(symbol=symbol, interval="60", limit=count)
            if hasattr(result, "__await__"):
                result = await result
            closes = self._extract_closes(result)
            if closes:
                return closes
        except Exception as e:
            logger.debug("%s kwargs interval=60 failed: %s", method_name, e)

        # Variant 3: kwargs interval="1h" (alt format)
        try:
            result = method(symbol=symbol, interval="1h", limit=count)
            if hasattr(result, "__await__"):
                result = await result
            closes = self._extract_closes(result)
            if closes:
                return closes
        except Exception as e:
            logger.debug("%s kwargs interval=1h failed: %s", method_name, e)

        return None

    @staticmethod
    def _extract_closes(raw) -> Optional[List[float]]:
        """Best-effort close-price extraction from various Bybit response shapes.

        Bybit V5 kline: [start, open, high, low, close, volume, turnover]
        Some wrappers return dicts: [{"close": "..."}]
        Some return responses: {"result": {"list": [...]}}
        """
        if raw is None:
            return None

        # Unwrap common envelopes
        if isinstance(raw, dict):
            for key in ("result", "data", "klines"):
                if key in raw:
                    raw = raw[key]
                    break
            if isinstance(raw, dict):
                for key in ("list", "items", "klines"):
                    if key in raw:
                        raw = raw[key]
                        break

        if not isinstance(raw, list) or not raw:
            return None

        closes: List[float] = []
        for entry in raw:
            try:
                if isinstance(entry, (list, tuple)) and len(entry) >= 5:
                    closes.append(float(entry[4]))
                elif isinstance(entry, dict):
                    val = (
                        entry.get("close")
                        or entry.get("c")
                        or entry.get("4")
                    )
                    if val is not None:
                        closes.append(float(val))
            except (ValueError, TypeError, IndexError):
                continue

        if not closes:
            return None

        # Bybit returns newest-first; we want oldest-first for RSI
        # Detect direction by checking timestamps if present, else assume newest-first
        if isinstance(raw[0], (list, tuple)) and len(raw[0]) >= 1:
            try:
                first_ts = int(raw[0][0])
                last_ts = int(raw[-1][0])
                if first_ts > last_ts:
                    closes.reverse()
            except (ValueError, TypeError, IndexError):
                closes.reverse()  # safe default
        else:
            closes.reverse()

        return closes


# ---------------------------------------------------------------------------
# Builder — constructs adapter from existing project tree (graceful fallback)
# ---------------------------------------------------------------------------

async def try_build_bybit_provider() -> Optional[BybitProvider]:
    """Attempt to wire up BybitProvider using the deployed BybitClient.

    Returns None on any failure (logs warning). bot_runner uses this in:

        provider = await try_build_bybit_provider()
        anti_bias = AntiBiasGate(provider) if provider else None

    The function tries multiple reasonable BybitClient init patterns
    because we don't have introspection of its exact signature.
    """
    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    testnet = os.getenv("BYBIT_TESTNET", "true").lower() in ("true", "1", "yes")

    # 1. Lazy import — fails gracefully if module structure changed
    try:
        from bot.core.bybit_client import BybitClient
    except ImportError as e:
        logger.warning("BybitClient import failed (%s) — anti-bias disabled", e)
        return None

    # 2. Try several constructor patterns
    candidates = [
        # Pattern A: kwargs (most likely modern API)
        lambda: BybitClient(api_key=api_key, api_secret=api_secret, testnet=testnet),
        # Pattern B: kwargs without testnet
        lambda: BybitClient(api_key=api_key, api_secret=api_secret),
        # Pattern C: no-args (uses env internally)
        lambda: BybitClient(),
        # Pattern D: positional
        lambda: BybitClient(api_key, api_secret, testnet),
    ]

    client = None
    last_error: Optional[Exception] = None
    for i, factory in enumerate(candidates, 1):
        try:
            client = factory()
            logger.info("BybitClient initialized via pattern %d", i)
            break
        except Exception as e:
            last_error = e
            logger.debug("BybitClient init pattern %d failed: %s", i, e)

    if client is None:
        logger.warning(
            "All BybitClient init patterns failed — anti-bias disabled. Last error: %s",
            last_error,
        )
        return None

    # 3. Probe with a known symbol to verify the API works
    provider = BybitProvider(client)
    try:
        probe = await provider.get_klines_1h("ETHUSDT", count=2)
        if probe is None or len(probe) < 2:
            logger.warning(
                "BybitProvider probe returned no data — likely API mismatch. "
                "Anti-bias disabled. To enable, verify BybitClient.get_klines() "
                "signature in bot/core/bybit_client.py"
            )
            return None
        logger.info(
            "BybitProvider verified: ETHUSDT 2-kline probe OK (latest close=%.2f)",
            probe[-1],
        )
        return provider
    except Exception as e:
        logger.warning("BybitProvider probe raised: %s — anti-bias disabled", e)
        return None
