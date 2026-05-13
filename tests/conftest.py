"""
LV1 — Test Fixtures
===================

Shared fixtures and test helpers for LV1 strategy tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Any

import pytest

from bot.strategies.lv1_models import (
    MarketState,
    QADirective,
    Direction,
    RollingStats,
    SweepSignal,
)
from bot.strategies.lv1_self_critique import CritiqueResult


# ─────────────────────────────────────────────────────────────────────────────
# Mock services
# ─────────────────────────────────────────────────────────────────────────────

class MockRiskKernel:
    def __init__(self, equity: float = 1000.0, daily_dd: float = 0.0, halted: bool = False):
        self.current_equity = equity
        self._daily_dd = daily_dd
        self._halted = halted
        self.trades: list[dict] = []

    def is_trading_allowed(self) -> bool:
        return not self._halted

    def daily_drawdown_pct(self) -> float:
        return self._daily_dd

    def record_trade(self, symbol: str, pnl_usd: float, was_win: bool) -> None:
        self.trades.append({"symbol": symbol, "pnl": pnl_usd, "win": was_win})


class MockLedger:
    def __init__(self, stats: Optional[RollingStats] = None):
        self._stats = stats or RollingStats()
        self.events: list[dict] = []
        self.paper_trades: list[dict] = []

    def rolling_stats(self, strategy: str, n: int = 100) -> RollingStats:
        return self._stats

    def log_paper_trade(self, strategy: str, signal: Any, size_usd: float) -> None:
        self.paper_trades.append({"strategy": strategy, "signal": signal, "size_usd": size_usd})

    def log_event(self, strategy: str, event_type: str, payload: Any) -> None:
        self.events.append({"strategy": strategy, "type": event_type, "payload": payload})


class MockBybitClient:
    def __init__(self, fail_orders: bool = False):
        self.fail_orders = fail_orders
        self.orders: list[dict] = []
        self.cancellations: list[dict] = []
        self._next_id = 1

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        if self.fail_orders:
            raise RuntimeError("forced failure")
        order = {
            "id": f"mock-{self._next_id}",
            "symbol": symbol, "type": type, "side": side,
            "amount": amount, "price": price, "params": params or {},
        }
        self._next_id += 1
        self.orders.append(order)
        return order

    async def cancel_order(self, order_id, symbol):
        self.cancellations.append({"id": order_id, "symbol": symbol})
        return {"id": order_id, "status": "canceled"}


# ─────────────────────────────────────────────────────────────────────────────
# Builders for canonical test states
# ─────────────────────────────────────────────────────────────────────────────

def build_market_state(
    *,
    symbol: str = "ETH/USDT:USDT",
    setup: str = "long_sweep",                  # "long_sweep" / "short_sweep" / "neutral"
    funding: float = -0.0002,
    spread_bps: float = 2.0,
    depth_10bps: float = 5_000_000.0,
    seconds_to_funding: int = 14400,
    btc_1m_return: float = 0.0,
    bvol: float = 50.0,
    dxy_intraday: float = 0.0,
    in_event: bool = False,
    btc_eth_corr: float = 0.85,
    oi_growth_24h: float = 5.0,
    atr_5m: float = 8.0,
    atr_1h: float = 25.0,
    atr_med_30d: float = 22.0,
    spot_basis_bps: float = 0.0,
    cvd_15m: float = 50.0,
    cvd_15m_at_prev_low: float = 10.0,
    cvd_60s: float = 20.0,
    cvd_med_30m: float = 0.0,
    cvd_mad_30m: float = 5.0,
) -> MarketState:
    """
    Build a parametric MarketState. Default setup ('long_sweep') passes E1 cleanly:
      swing_low_5m = 3500.0, prev_low_1m = 3492.0 (-22.8 bp proboj),
      prev_close_1m = 3501.5 (reclaim), all gates clear.
    """
    base_price = 3500.0
    if setup == "long_sweep":
        swing_low = base_price
        swing_high = base_price * 1.01
        prev_low = base_price * 0.998              # -20bp proboj
        prev_high = base_price * 1.005
        prev_close = base_price * 1.0005           # reclaim
        last_low = base_price * 0.999              # earlier candles higher than prev
        last_high = base_price * 1.0035
        last_close = base_price * 1.001
        price = base_price * 1.0008
    elif setup == "short_sweep":
        swing_low = base_price * 0.99
        swing_high = base_price
        prev_low = base_price * 0.995
        prev_high = base_price * 1.002             # +20bp above swing high
        prev_close = base_price * 0.9995           # reclaim below
        last_low = base_price * 0.9965
        last_high = base_price * 1.001
        last_close = base_price * 0.999
        price = base_price * 0.9992
    else:
        swing_low = base_price * 0.99
        swing_high = base_price * 1.01
        prev_low = base_price * 0.998
        prev_high = base_price * 1.002
        prev_close = base_price
        last_low = base_price * 0.999
        last_high = base_price * 1.001
        last_close = base_price
        price = base_price

    spot_price = price / (1.0 + spot_basis_bps / 10_000.0)

    return MarketState(
        symbol=symbol,
        price=price,
        spot_price=spot_price,
        funding_rate=funding,
        seconds_to_funding=seconds_to_funding,
        atr_5m=atr_5m,
        atr_1h=atr_1h,
        atr_median_30d=atr_med_30d,
        swing_low_5m=swing_low,
        swing_high_5m=swing_high,
        last_low_1m=last_low,
        last_high_1m=last_high,
        last_close_1m=last_close,
        prev_low_1m=prev_low,
        prev_high_1m=prev_high,
        prev_close_1m=prev_close,
        cvd_15m=cvd_15m,
        cvd_15m_at_prev_low=cvd_15m_at_prev_low,
        cvd_60s=cvd_60s,
        cvd_rolling_median_30m=cvd_med_30m,
        cvd_rolling_mad_30m=cvd_mad_30m,
        spread_bps=spread_bps,
        depth_10bps_usd=depth_10bps,
        book_bid=price * 0.9999,
        book_ask=price * 1.0001,
        btc_1m_return=btc_1m_return,
        btc_eth_corr_60d=btc_eth_corr,
        oi_growth_24h_pct=oi_growth_24h,
        bvol_index=bvol,
        dxy_intraday_pct=dxy_intraday,
        in_calendar_event_window=in_event,
        timestamp=datetime.now(timezone.utc),
    )


def build_qa(
    *,
    direction: str = "LONG",
    s13: bool = False,
    vix: float = 20.0,
    regime: str = "NEUTRAL",
    top_wrong: int = 0,
) -> QADirective:
    return QADirective(
        asset="ETH/USDT",
        direction=direction,
        s13_active=s13,
        vix_level=vix,
        regime=regime,
        top_wrong_count=top_wrong,
    )


# ─────────────────────────────────────────────────────────────────────────────
# pytest fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def risk_kernel() -> MockRiskKernel:
    return MockRiskKernel(equity=1000.0)


@pytest.fixture
def ledger() -> MockLedger:
    return MockLedger(stats=RollingStats(n_trades=80, wins=34, avg_win_R=2.5, avg_loss_R=1.0))


@pytest.fixture
def bybit_client() -> MockBybitClient:
    return MockBybitClient()


@pytest.fixture
def good_long_state() -> MarketState:
    """Canonical clean LONG sweep setup, all gates pass, no red flags."""
    return build_market_state(setup="long_sweep")


@pytest.fixture
def good_short_state() -> MarketState:
    return build_market_state(
        setup="short_sweep",
        funding=+0.0002,
    )


@pytest.fixture
def neutral_qa() -> QADirective:
    return build_qa(direction="LONG")
