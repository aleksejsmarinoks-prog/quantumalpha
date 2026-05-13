"""
LV1 — Orderbook Metrics
=======================

Pure functions to compute orderbook-derived signals from raw L2 snapshots.

Snapshot format (CCXT-compatible):
    {
        "bids": [[price, size], ...],   # descending
        "asks": [[price, size], ...],   # ascending
        "timestamp": int_ms,
    }

All functions are pure — no I/O, no async — for trivial unit testing.

Author: QuantForge / QuantumAlpha
Phase: 6.1
"""
from __future__ import annotations

from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Basic metrics
# ─────────────────────────────────────────────────────────────────────────────

def best_bid_ask(book: dict) -> tuple[Optional[float], Optional[float]]:
    """Return (best_bid, best_ask). None for empty side."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    return bid, ask


def mid_price(book: dict) -> Optional[float]:
    """Return arithmetic mid price, or None if book empty on either side."""
    bid, ask = best_bid_ask(book)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def spread_bps(book: dict) -> Optional[float]:
    """Return bid-ask spread in basis points, None if book empty."""
    bid, ask = best_bid_ask(book)
    if bid is None or ask is None or bid <= 0:
        return None
    return ((ask - bid) / bid) * 10_000.0


# ─────────────────────────────────────────────────────────────────────────────
# Depth aggregations
# ─────────────────────────────────────────────────────────────────────────────

def depth_within_bps(book: dict, bps: float, side: str = "both") -> float:
    """
    Total USD notional within `bps` of mid price.

    side: 'bid' | 'ask' | 'both' (default).
    Returns 0.0 if book empty or side unknown.
    """
    mid = mid_price(book)
    if mid is None:
        return 0.0
    range_pct = bps / 10_000.0
    floor = mid * (1.0 - range_pct)
    ceil = mid * (1.0 + range_pct)

    notional = 0.0
    if side in ("bid", "both"):
        for price, size in (book.get("bids") or []):
            if price >= floor:
                notional += price * size
            else:
                break
    if side in ("ask", "both"):
        for price, size in (book.get("asks") or []):
            if price <= ceil:
                notional += price * size
            else:
                break
    return notional


def depth_10bps_usd(book: dict) -> float:
    """Convenience: total USD notional within 10bps of mid (both sides)."""
    return depth_within_bps(book, 10.0, "both")


# ─────────────────────────────────────────────────────────────────────────────
# Imbalance
# ─────────────────────────────────────────────────────────────────────────────

def order_book_imbalance(book: dict, levels: int = 5) -> float:
    """
    OBI for top `levels`:  (sum_bid_size - sum_ask_size) / (sum_bid_size + sum_ask_size)

    Range: [-1, +1]. Positive → bids dominate. Returns 0.0 for empty book.
    """
    bids = (book.get("bids") or [])[:levels]
    asks = (book.get("asks") or [])[:levels]
    bid_total = sum(size for _, size in bids)
    ask_total = sum(size for _, size in asks)
    denom = bid_total + ask_total
    if denom <= 0:
        return 0.0
    return (bid_total - ask_total) / denom


def book_health_check(book: dict, max_spread_bps: float = 10.0, min_depth_usd: float = 100_000.0) -> bool:
    """
    Sanity check: spread and depth both healthy.
    Used by self-critique factor #8 and main pre-entry gate.
    """
    sb = spread_bps(book)
    if sb is None or sb > max_spread_bps:
        return False
    if depth_10bps_usd(book) < min_depth_usd:
        return False
    return True
