"""
QA Trade Trigger — Asset Mapping
=================================

Maps classified event types to specific tickers, directions, and conviction.

CRITICAL constraints from QA protocol:
  - Excluded from all portfolios: BTC, COPX, URA, KTOS (no signals generated)
  - QA tradeable T212 ETFs: IGLN.L, DFNS.L, DFNG.L, SHEL.L, SWDA.L, IBTS.L, SSLN.L
  - QuantForge crypto: ETH/USDT, SOL/USDT (Bybit perpetuals)
  - BTC may appear ONLY as regime indicator, never as buy signal

Conviction scale:
  0.0-0.3: weak / situational
  0.3-0.6: moderate / DCA-only
  0.6-0.8: strong / standard sizing
  0.8-1.0: high / max sizing within bucket cap

Suggested size = conviction × bucket_cap_remaining. Hard caps from QA never violated.

Author: QuantumAlpha
Version: 0.1.0
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from .models import Direction, AssetTrigger


# ---------------------------------------------------------------------------
# Hard exclusion list (QA protocol)
# ---------------------------------------------------------------------------

EXCLUDED_TICKERS = frozenset({"BTC", "BTC/USDT", "COPX", "URA", "KTOS"})


# ---------------------------------------------------------------------------
# Event → Asset mapping
# ---------------------------------------------------------------------------
#
# Format per entry:
#   (ticker, venue, direction, conviction, half_life_minutes, invalidation_hint)
#
# Half-life = expected window during which 80% of price reaction completes.
# Below half-life — trade still actionable. After 2× half-life — usually too late.

EventMapping = List[Tuple[str, str, Direction, float, int, Optional[str]]]

EVENT_ASSET_MAPPING: Dict[str, EventMapping] = {

    # =========================================================================
    # GEOPOLITICS — Middle East
    # =========================================================================
    "hormuz_easing": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.75, 120, "ETH below pre-event level"),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.65, 90, "SOL below pre-event level"),
        ("SHEL.L", "T212", Direction.SHORT, 0.45, 240, "Oil rebounds above pre-event"),
        ("IGLN.L", "T212", Direction.SHORT, 0.40, 240, "Gold rebounds, deal collapses"),
    ],
    "hormuz_escalation": [
        ("SHEL.L", "T212", Direction.LONG, 0.75, 180, "Hormuz reopens fully"),
        ("IGLN.L", "T212", Direction.LONG, 0.70, 240, "VIX retraces below 22"),
        ("DFNS.L", "T212", Direction.LONG, 0.60, 360, "Ceasefire announced"),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.50, 90, "Risk-off resolves"),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.55, 90, "Risk-off resolves"),
    ],
    "iran_us_deal_signal": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.70, 180, "Deal falls through"),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.65, 120, "Deal falls through"),
        ("IGLN.L", "T212", Direction.SHORT, 0.40, 240, None),
    ],
    "iran_us_breakdown": [
        ("IGLN.L", "T212", Direction.LONG, 0.70, 180, None),
        ("DFNS.L", "T212", Direction.LONG, 0.55, 360, None),
        ("SHEL.L", "T212", Direction.LONG, 0.65, 180, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.45, 90, None),
    ],
    "middle_east_strike": [
        ("DFNS.L", "T212", Direction.LONG, 0.75, 240, "Ceasefire within 24h"),
        ("IGLN.L", "T212", Direction.LONG, 0.70, 240, None),
        ("SHEL.L", "T212", Direction.LONG, 0.65, 180, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 60, None),
    ],

    # =========================================================================
    # GEOPOLITICS — Russia / Ukraine
    # =========================================================================
    "russia_ukraine_escalation": [
        ("DFNG.L", "T212", Direction.LONG, 0.75, 360, "Ceasefire announced"),
        ("DFNS.L", "T212", Direction.LONG, 0.70, 360, None),
        ("IGLN.L", "T212", Direction.LONG, 0.55, 240, None),
        ("SHEL.L", "T212", Direction.LONG, 0.50, 240, None),
    ],
    "russia_ukraine_ceasefire": [
        ("DFNG.L", "T212", Direction.SHORT, 0.65, 480, "Ceasefire breaks"),
        ("DFNS.L", "T212", Direction.SHORT, 0.50, 480, None),
        ("ETH/USDT", "Bybit", Direction.LONG, 0.55, 180, None),
        ("SWDA.L", "T212", Direction.LONG, 0.50, 480, None),
    ],

    # =========================================================================
    # GEOPOLITICS — China / Taiwan
    # =========================================================================
    "china_taiwan_tension": [
        ("DFNS.L", "T212", Direction.LONG, 0.65, 360, None),
        ("IGLN.L", "T212", Direction.LONG, 0.55, 360, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 120, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.60, 120, None),
    ],
    "china_taiwan_deescalation": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.55, 180, None),
        ("SWDA.L", "T212", Direction.LONG, 0.45, 480, None),
        ("DFNS.L", "T212", Direction.SHORT, 0.40, 720, None),
    ],
    "us_china_trade_escalation": [
        ("IGLN.L", "T212", Direction.LONG, 0.60, 360, None),
        ("DFNS.L", "T212", Direction.LONG, 0.45, 480, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.50, 120, None),
    ],

    # =========================================================================
    # MACRO — Fed / Monetary policy
    # =========================================================================
    "fed_dovish_signal": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.80, 240, "Reversal hawkish comment"),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.70, 180, None),
        ("IGLN.L", "T212", Direction.LONG, 0.65, 360, None),
        ("IBTS.L", "T212", Direction.LONG, 0.50, 480, None),
        ("SWDA.L", "T212", Direction.LONG, 0.55, 480, None),
    ],
    "fed_hawkish_signal": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.65, 180, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.70, 120, None),
        ("IGLN.L", "T212", Direction.SHORT, 0.45, 360, None),
        ("IBTS.L", "T212", Direction.SHORT, 0.55, 480, None),
    ],
    "fed_rate_cut_surprise": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.85, 240, None),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.80, 180, None),
        ("IGLN.L", "T212", Direction.LONG, 0.70, 480, None),
        ("SWDA.L", "T212", Direction.LONG, 0.65, 480, None),
    ],
    "fed_chair_dovish_speech": [   # specifically Warsh / new chair signals
        ("ETH/USDT", "Bybit", Direction.LONG, 0.75, 180, None),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.65, 120, None),
        ("IGLN.L", "T212", Direction.LONG, 0.55, 360, None),
    ],
    "treasury_yield_spike": [
        ("IBTS.L", "T212", Direction.SHORT, 0.65, 360, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 120, None),
        ("IGLN.L", "T212", Direction.SHORT, 0.40, 360, None),
    ],

    # =========================================================================
    # MACRO — Inflation / employment
    # =========================================================================
    "cpi_hot": [
        ("IGLN.L", "T212", Direction.LONG, 0.55, 240, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.50, 120, None),
        ("IBTS.L", "T212", Direction.SHORT, 0.65, 360, None),
    ],
    "cpi_cool": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.75, 180, None),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.70, 180, None),
        ("SWDA.L", "T212", Direction.LONG, 0.60, 480, None),
        ("IBTS.L", "T212", Direction.LONG, 0.55, 480, None),
    ],
    "nfp_hot": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 120, None),
        ("IBTS.L", "T212", Direction.SHORT, 0.50, 360, None),
    ],
    "nfp_weak": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.65, 180, None),
        ("IGLN.L", "T212", Direction.LONG, 0.55, 360, None),
    ],

    # =========================================================================
    # CRYPTO-SPECIFIC
    # =========================================================================
    "spot_etf_inflow_record": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.65, 180, None),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.55, 180, None),
    ],
    "spot_etf_outflow_record": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.65, 180, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.70, 180, None),
    ],
    "ofac_crypto_sanctions": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 90, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.60, 90, None),
    ],
    "sec_crypto_lawsuit_major": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.65, 120, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.75, 120, None),
    ],
    "sec_crypto_favorable_ruling": [
        ("ETH/USDT", "Bybit", Direction.LONG, 0.70, 180, None),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.75, 180, None),
    ],
    "exchange_hack_major": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.60, 60, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.65, 60, None),
    ],
    "stablecoin_depeg": [
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.75, 60, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.80, 60, None),
        ("IGLN.L", "T212", Direction.LONG, 0.55, 240, None),
    ],
    "btc_etf_approval_altcoin": [   # ETH/SOL ETF approval
        ("ETH/USDT", "Bybit", Direction.LONG, 0.80, 240, None),
        ("SOL/USDT", "Bybit", Direction.LONG, 0.85, 240, None),
    ],

    # =========================================================================
    # COMMODITIES / SHIPPING (second-order opportunities per QA protocol)
    # =========================================================================
    "oil_supply_disruption": [
        ("SHEL.L", "T212", Direction.LONG, 0.70, 240, None),
        ("IGLN.L", "T212", Direction.LONG, 0.45, 360, None),
    ],
    "shipping_disruption": [
        # SHEL benefits, also signals shipping stocks (FRO, STNG, INSW from QA memo)
        ("SHEL.L", "T212", Direction.LONG, 0.55, 360, None),
        # Note: FRO/STNG/INSW not in QA portfolio but flagged for opportunity scan
    ],

    # =========================================================================
    # SYSTEMIC / TAIL RISK
    # =========================================================================
    "bank_failure_major": [
        ("IGLN.L", "T212", Direction.LONG, 0.75, 360, None),
        ("IBTS.L", "T212", Direction.LONG, 0.65, 360, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 60, "Initial flush only"),
        ("SWDA.L", "T212", Direction.SHORT, 0.50, 240, None),
    ],
    "vix_spike_extreme": [
        ("IGLN.L", "T212", Direction.LONG, 0.60, 240, None),
        ("ETH/USDT", "Bybit", Direction.SHORT, 0.55, 60, None),
        ("SOL/USDT", "Bybit", Direction.SHORT, 0.65, 60, None),
    ],
}


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

def get_triggers_for_event(
    event_type: str,
    bucket_cap_pct: float = 100.0,
) -> List[AssetTrigger]:
    """
    Resolve event_type → list of AssetTrigger, applying:
      - Exclusion filter (BTC/COPX/URA/KTOS removed)
      - bucket_cap_pct rescaling (suggested_size = conviction × bucket_cap)

    Returns empty list if event_type unknown.
    """
    raw = EVENT_ASSET_MAPPING.get(event_type, [])
    triggers: List[AssetTrigger] = []
    for ticker, venue, direction, conviction, half_life, inv_reason in raw:
        if ticker in EXCLUDED_TICKERS:
            continue
        size = round(conviction * bucket_cap_pct, 2)
        triggers.append(AssetTrigger(
            ticker=ticker,
            venue=venue,
            direction=direction,
            conviction=conviction,
            suggested_size_pct_bucket=size,
            invalidation_price=None,
            invalidation_reason=inv_reason,
            half_life_minutes=half_life,
        ))
    return triggers


def list_supported_events() -> List[str]:
    return sorted(EVENT_ASSET_MAPPING.keys())


def is_event_supported(event_type: str) -> bool:
    return event_type in EVENT_ASSET_MAPPING


def get_max_half_life(event_type: str) -> int:
    """Max half_life across all triggers for this event (used by velocity gate)."""
    triggers = EVENT_ASSET_MAPPING.get(event_type, [])
    if not triggers:
        return 0
    return max(t[4] for t in triggers)
