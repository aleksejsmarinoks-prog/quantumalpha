"""
core/bybit_client.py — QuantumAlpha Bybit Client v1.0

Unified wrapper around Bybit V5 API for QA system.
Combines: public REST (no auth) + private REST (auth) + WebSocket streams.

Design principles:
  1. Public endpoints work WITHOUT API keys (funding rates, kline, orderbook)
  2. Private endpoints require BYBIT_API_KEY + BYBIT_API_SECRET in env
  3. Async-first: all I/O via aiohttp + ccxt.async_support
  4. Rate-limit aware: respects X-Bapi-Limit-Status headers
  5. Earn endpoints: placeholders, not used in trading; require manual verification
     against latest Bybit dev docs before production use.

Reference: https://bybit-exchange.github.io/docs/v5/intro
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

log = logging.getLogger("qa_bot.bybit_client")


# =============================================================================
# CONSTANTS
# =============================================================================

REST_BASE_URL      = "https://api.bybit.com"
WS_PUBLIC_LINEAR   = "wss://stream.bybit.com/v5/public/linear"
WS_PUBLIC_SPOT     = "wss://stream.bybit.com/v5/public/spot"
WS_PRIVATE         = "wss://stream.bybit.com/v5/private"

DEFAULT_RECV_WINDOW = "5000"   # ms
DEFAULT_TIMEOUT     = 15       # seconds
RATE_LIMIT_BUFFER   = 5        # leave this many requests untouched


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class FundingRate:
    """Funding rate snapshot for a USDT perpetual."""
    symbol:                 str           # 'ETHUSDT'
    funding_rate:           float         # 0.0001 = 0.01% per 8h
    next_funding_time_ms:   int
    fetched_at_utc:         float         # time.time()

    @property
    def annualized_pct(self) -> float:
        """Convert per-8h rate to APR. 3 settlements/day × 365."""
        return self.funding_rate * 3 * 365 * 100


@dataclass
class Ticker:
    symbol:        str
    last_price:    float
    bid:           float
    ask:           float
    volume_24h:    float
    high_24h:      float
    low_24h:       float
    fetched_at:    float


@dataclass
class Kline:
    """Single candle."""
    open_time_ms:  int
    open:          float
    high:          float
    low:           float
    close:         float
    volume:        float
    turnover:      float

    @classmethod
    def from_bybit_v5(cls, raw: list) -> "Kline":
        """Bybit V5 kline format: [start, open, high, low, close, volume, turnover]"""
        return cls(
            open_time_ms=int(raw[0]),
            open=float(raw[1]), high=float(raw[2]),
            low=float(raw[3]),  close=float(raw[4]),
            volume=float(raw[5]), turnover=float(raw[6]),
        )


# =============================================================================
# BYBIT CLIENT
# =============================================================================

class BybitClient:
    """
    Unified Bybit V5 client. Public endpoints work without keys.
    Set BYBIT_API_KEY + BYBIT_API_SECRET in env for private endpoints.

    Usage:
        async with BybitClient() as client:
            fr = await client.fetch_funding_rate("ETHUSDT")
            print(fr.annualized_pct)
    """

    def __init__(
        self,
        api_key:    Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet:    bool = False,
    ):
        self.api_key    = api_key    or os.getenv("BYBIT_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BYBIT_API_SECRET", "")
        self.has_auth   = bool(self.api_key and self.api_secret)
        self.testnet    = testnet
        self.base_url   = (
            "https://api-testnet.bybit.com" if testnet else REST_BASE_URL
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._rate_status: dict[str, int] = {}

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()
            self._session = None

    async def _ensure_session(self) -> None:
        """Lazily create the aiohttp session. Safe to call from any HTTP method.
        Supports both `async with BybitClient()` and direct construction."""
        if self._session is not None and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        )
        if self.has_auth:
            log.info(f"BybitClient: authenticated mode (key={self.api_key[:8]}...)")
        else:
            log.info("BybitClient: public-only mode (no API keys)")

    # ── PRIVATE: SIGNING ────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, params: str) -> str:
        """V5 signature: HMAC-SHA256(timestamp + api_key + recv_window + params)"""
        sign_string = f"{timestamp}{self.api_key}{DEFAULT_RECV_WINDOW}{params}"
        return hmac.new(
            self.api_secret.encode(),
            sign_string.encode(),
            hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, params: str) -> dict[str, str]:
        if not self.has_auth:
            raise RuntimeError("Auth required but BYBIT_API_KEY/BYBIT_API_SECRET not set")
        timestamp = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY":     self.api_key,
            "X-BAPI-TIMESTAMP":   timestamp,
            "X-BAPI-SIGN":        self._sign(timestamp, params),
            "X-BAPI-RECV-WINDOW": DEFAULT_RECV_WINDOW,
            "Content-Type":       "application/json",
        }

    # ── PUBLIC: REST WRAPPER ────────────────────────────────────────────────────

    async def public_get(self, endpoint: str, params: dict = None) -> dict:
        """GET request to a public endpoint (no auth)."""
        await self._ensure_session()
        params = params or {}
        url = f"{self.base_url}{endpoint}"
        async with self._session.get(url, params=params) as r:
            text = await r.text()
            self._update_rate_status(r.headers, endpoint)
            if r.status != 200:
                raise RuntimeError(
                    f"Bybit HTTP {r.status} on {endpoint}: {text[:200]}"
                )
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Bybit returned non-JSON on {endpoint}: {text[:200]}"
                ) from e
            if data.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit API error {data.get('retCode')}: {data.get('retMsg')} "
                    f"[endpoint={endpoint}]"
                )
            return data.get("result", {})

    async def private_get(self, endpoint: str, params: dict = None) -> dict:
        await self._ensure_session()
        params = params or {}
        param_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
        headers   = self._auth_headers(param_str)
        url       = f"{self.base_url}{endpoint}"
        async with self._session.get(url, params=params, headers=headers) as r:
            data = await r.json()
            self._update_rate_status(r.headers, endpoint)
            if data.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}"
                )
            return data.get("result", {})

    async def private_post(self, endpoint: str, payload: dict) -> dict:
        await self._ensure_session()
        body    = json.dumps(payload, separators=(",", ":"))
        headers = self._auth_headers(body)
        url     = f"{self.base_url}{endpoint}"
        async with self._session.post(url, data=body, headers=headers) as r:
            data = await r.json()
            self._update_rate_status(r.headers, endpoint)
            if data.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}"
                )
            return data.get("result", {})

    def _update_rate_status(self, headers: dict, endpoint: str):
        try:
            status = headers.get("X-Bapi-Limit-Status")
            if status is not None:
                self._rate_status[endpoint] = int(status)
                if int(status) <= RATE_LIMIT_BUFFER:
                    log.warning(f"Rate limit low: {endpoint} = {status} remaining")
        except (ValueError, TypeError):
            pass

    # ── PUBLIC ENDPOINTS — No auth required ─────────────────────────────────────

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """
        Get current funding rate for a USDT perpetual (linear).
        Symbol format: 'ETHUSDT' (no slash).
        """
        result = await self.public_get(
            "/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
        )
        ticker_list = result.get("list", [])
        if not ticker_list:
            raise RuntimeError(f"No ticker data for {symbol}")
        t = ticker_list[0]
        return FundingRate(
            symbol=symbol,
            funding_rate=float(t["fundingRate"]),
            next_funding_time_ms=int(t["nextFundingTime"]),
            fetched_at_utc=time.time(),
        )

    async def fetch_funding_rate_history(
        self, symbol: str, limit: int = 200
    ) -> list[dict]:
        """Historical funding rates for analysis. Up to 200 records."""
        result = await self.public_get(
            "/v5/market/funding/history",
            params={"category": "linear", "symbol": symbol, "limit": limit},
        )
        return result.get("list", [])

    async def fetch_ticker(self, symbol: str, category: str = "linear") -> Ticker:
        """Get current ticker. Category: 'linear' (perp) or 'spot'."""
        result = await self.public_get(
            "/v5/market/tickers",
            params={"category": category, "symbol": symbol},
        )
        t = result.get("list", [{}])[0]
        if not t:
            raise RuntimeError(f"No ticker for {symbol}")
        return Ticker(
            symbol=symbol,
            last_price=float(t.get("lastPrice", 0)),
            bid=float(t.get("bid1Price", 0)),
            ask=float(t.get("ask1Price", 0)),
            volume_24h=float(t.get("volume24h", 0)),
            high_24h=float(t.get("highPrice24h", 0)),
            low_24h=float(t.get("lowPrice24h", 0)),
            fetched_at=time.time(),
        )

    async def fetch_kline(
        self, symbol: str, interval: str = "60",
        limit: int = 200, category: str = "linear",
    ) -> list[Kline]:
        """
        Historical candles.
        Interval: '1', '3', '5', '15', '30', '60', '120', '240', 'D', 'W', 'M'.
        """
        result = await self.public_get(
            "/v5/market/kline",
            params={
                "category": category, "symbol": symbol,
                "interval": interval, "limit": limit,
            },
        )
        raw_list = result.get("list", [])
        # Bybit returns newest first; reverse for chronological order
        return [Kline.from_bybit_v5(r) for r in reversed(raw_list)]

    async def get_klines(
        self, category: str = "linear", symbol: str = "",
        interval: str = "60", limit: int = 200,
    ) -> list[list[str]]:
        """
        Raw V5 klines, chronological (oldest first).
        Each row: [start_ms, open, high, low, close, volume, turnover] as strings.
        """
        result = await self.public_get(
            "/v5/market/kline",
            params={
                "category": category, "symbol": symbol,
                "interval": interval, "limit": limit,
            },
        )
        return list(reversed(result.get("list", [])))

    async def fetch_orderbook(
        self, symbol: str, limit: int = 50, category: str = "linear"
    ) -> dict:
        """Order book snapshot. Returns {bids, asks, timestamp}."""
        result = await self.public_get(
            "/v5/market/orderbook",
            params={"category": category, "symbol": symbol, "limit": limit},
        )
        return {
            "bids": [[float(p), float(q)] for p, q in result.get("b", [])],
            "asks": [[float(p), float(q)] for p, q in result.get("a", [])],
            "timestamp_ms": result.get("ts", 0),
        }

    async def fetch_instruments_info(self, category: str = "linear") -> list[dict]:
        """All trading symbols + their specs (min qty, tick size, etc)."""
        result = await self.public_get(
            "/v5/market/instruments-info",
            params={"category": category},
        )
        return result.get("list", [])

    # ── PRIVATE ENDPOINTS — Require API keys ────────────────────────────────────

    async def fetch_balance(self, account_type: str = "UNIFIED") -> dict:
        """
        Account balance. account_type: UNIFIED, SPOT, FUND.
        Returns dict with coin balances.
        """
        result = await self.private_get(
            "/v5/account/wallet-balance",
            params={"accountType": account_type},
        )
        accts = result.get("list", [])
        if not accts:
            return {}
        coins = accts[0].get("coin", [])
        return {
            c["coin"]: {
                "wallet_balance":   float(c.get("walletBalance", 0) or 0),
                "available_balance": float(c.get("availableToWithdraw", 0) or 0),
                "usd_value":        float(c.get("usdValue", 0) or 0),
                "equity":           float(c.get("equity", 0) or 0),
            }
            for c in coins
        }

    async def get_unified_balance(self) -> dict:
        """
        Account-level UNIFIED wallet snapshot.
        Returns the raw V5 account dict with keys like totalEquity,
        totalWalletBalance, totalAvailableBalance, totalMarginBalance, coin[].
        Empty dict if account list is empty.
        """
        result = await self.private_get(
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
        )
        accts = result.get("list", [])
        return accts[0] if accts else {}

    async def fetch_positions(
        self, category: str = "linear", symbol: Optional[str] = None
    ) -> list[dict]:
        """Open positions. category='linear' for perps."""
        params = {"category": category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        result = await self.private_get("/v5/position/list", params=params)
        return result.get("list", [])

    async def place_order(
        self,
        category:    str,           # 'spot' | 'linear'
        symbol:      str,
        side:        str,           # 'Buy' | 'Sell'
        order_type:  str,           # 'Market' | 'Limit'
        qty:         str,           # always string per Bybit spec
        price:       Optional[str] = None,
        time_in_force: str = "GTC",
        order_link_id: Optional[str] = None,
        reduce_only: bool = False,
    ) -> dict:
        """
        Place an order. ALWAYS use string for qty/price (Bybit requirement).
        Returns order details with orderId.
        """
        payload = {
            "category":   category,
            "symbol":     symbol,
            "side":       side,
            "orderType":  order_type,
            "qty":        str(qty),
            "timeInForce": time_in_force,
            "reduceOnly": reduce_only,
        }
        if price is not None:
            payload["price"] = str(price)
        if order_link_id:
            payload["orderLinkId"] = order_link_id

        log.info(f"Placing order: {payload}")
        return await self.private_post("/v5/order/create", payload)

    async def cancel_order(
        self, category: str, symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
    ) -> dict:
        payload = {"category": category, "symbol": symbol}
        if order_id:
            payload["orderId"] = order_id
        elif order_link_id:
            payload["orderLinkId"] = order_link_id
        else:
            raise ValueError("Must provide order_id or order_link_id")
        return await self.private_post("/v5/order/cancel", payload)

    async def fetch_open_orders(
        self, category: str = "linear", symbol: Optional[str] = None
    ) -> list[dict]:
        params = {"category": category}
        if symbol:
            params["symbol"] = symbol
        result = await self.private_get("/v5/order/realtime", params=params)
        return result.get("list", [])

    # ── EARN ENDPOINTS — VERIFIED per DeepSeek Task #8 (2026-04-27) ────────────
    # Source: https://bybit-exchange.github.io/docs/v5/finance/earn/easy-onchain
    #
    # ✅ /v5/earn/product       (GET, no auth) — list products
    # ✅ /v5/earn/place-order   (POST, auth)   — stake / redeem
    # ⚠️  Position query: not exposed as standalone endpoint; must derive from
    #     /v5/account/transaction-log filtered by EARN type, or track via ledger.
    #
    # Categories: "FlexibleSaving" | "OnChain"
    # Order types: "Stake" | "Redeem"
    # Account types: "FUND" | "UNIFIED"

    async def list_earn_products(
        self, category: str = "FlexibleSaving", coin: Optional[str] = None
    ) -> list[dict]:
        """
        GET /v5/earn/product — list available Earn products.
        Returns: list of {productId, coin, estimateApr, minStakeAmount,
                          maxStakeAmount, status, ...}
        """
        params = {"category": category}
        if coin:
            params["coin"] = coin.upper()
        try:
            result = await self.public_get("/v5/earn/product", params=params)
            return result.get("list", [])
        except Exception as e:
            log.error(f"list_earn_products error: {e}")
            return []

    async def place_earn_order(
        self,
        category:           str,                # "FlexibleSaving" | "OnChain"
        order_type:         str,                # "Stake" | "Redeem"
        account_type:       str,                # "FUND" | "UNIFIED"
        amount:             str,                # As string per Bybit spec
        coin:               str,
        product_id:         str,
        order_link_id:      str,                # Required, max 36 chars, unique 30min
        redeem_position_id: Optional[str] = None,
        to_account_type:    Optional[str] = None,
    ) -> dict:
        """
        POST /v5/earn/place-order — stake or redeem an Earn product.

        Returns: {"orderId": "...", "orderLinkId": "..."}
        Raises on API error.
        """
        body = {
            "category":      category,
            "orderType":     order_type,
            "accountType":   account_type,
            "amount":        amount,
            "coin":          coin.upper(),
            "productId":     product_id,
            "orderLinkId":   order_link_id,
        }
        if redeem_position_id:
            body["redeemPositionId"] = redeem_position_id
        if to_account_type:
            body["toAccountType"] = to_account_type

        log.info(
            f"place_earn_order: {category} {order_type} {amount} {coin} "
            f"productId={product_id}"
        )
        result = await self.private_post("/v5/earn/place-order", body=body)
        return result

    # Backwards-compat alias for old name
    async def fetch_earn_products(self) -> list[dict]:
        return await self.list_earn_products()

    # ── HELPERS ─────────────────────────────────────────────────────────────────

    async def server_time(self) -> int:
        """Verify clock sync — important for signature timestamps."""
        result = await self.public_get("/v5/market/time")
        return int(result.get("timeSecond", 0))

    def get_rate_status(self) -> dict[str, int]:
        return dict(self._rate_status)


# =============================================================================
# CLI / SMOKE TEST
# =============================================================================

async def _smoke_test():
    """
    Tests public endpoints (no keys needed).
    Run: python bot/core/bybit_client.py
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    async with BybitClient() as client:
        # Test 1: server time
        ts = await client.server_time()
        print(f"\n✓ Server time: {ts} ({time.time() - ts:.0f}s offset)")

        # Test 2: funding rates for our targets
        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            fr = await client.fetch_funding_rate(symbol)
            print(
                f"✓ {symbol} funding: {fr.funding_rate*100:+.4f}%/8h "
                f"= {fr.annualized_pct:+.2f}% APR"
            )

        # Test 3: ticker
        t = await client.fetch_ticker("ETHUSDT")
        print(
            f"\n✓ ETHUSDT ticker: last=${t.last_price:,.2f} "
            f"bid=${t.bid:,.2f} ask=${t.ask:,.2f} "
            f"vol24h={t.volume_24h:,.0f}"
        )

        # Test 4: kline (last 5 hourly candles)
        klines = await client.fetch_kline("ETHUSDT", interval="60", limit=5)
        print(f"\n✓ ETHUSDT 1h candles ({len(klines)}):")
        for k in klines:
            ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(k.open_time_ms / 1000))
            print(f"    {ts}  O:{k.open:.2f}  H:{k.high:.2f}  "
                  f"L:{k.low:.2f}  C:{k.close:.2f}  V:{k.volume:.1f}")

        # Test 5: orderbook
        ob = await client.fetch_orderbook("ETHUSDT", limit=5)
        print(f"\n✓ ETHUSDT top of book:")
        print(f"    Asks: {ob['asks'][:3]}")
        print(f"    Bids: {ob['bids'][:3]}")

        # Test 6: historical funding rates (for baseline analysis)
        hist = await client.fetch_funding_rate_history("ETHUSDT", limit=10)
        print(f"\n✓ ETHUSDT funding history (last 10 settlements):")
        for h in hist[:5]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(h["fundingRateTimestamp"]) / 1000))
            rate = float(h["fundingRate"]) * 100
            print(f"    {ts}  rate={rate:+.4f}%/8h")

        print("\n✅ All public endpoint tests passed.")
        print("   (Auth tests skipped — no API keys in env)")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
