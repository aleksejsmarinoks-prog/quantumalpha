"""Asset mapping integrity tests."""

from __future__ import annotations

import pytest

from bot.trade_trigger.trade_trigger_mapping import (
    EVENT_ASSET_MAPPING, EXCLUDED_TICKERS,
    get_triggers_for_event, list_supported_events,
    is_event_supported, get_max_half_life,
)
from bot.trade_trigger.models import Direction


# QA-tradeable universe per memory (BTC/COPX/URA/KTOS excluded)
ALLOWED_TICKERS = {
    # T212 ETFs
    "IGLN.L", "DFNS.L", "DFNG.L", "SHEL.L", "SWDA.L", "IBTS.L", "SSLN.L",
    # Bybit perpetuals (BTC excluded)
    "ETH/USDT", "SOL/USDT",
}


class TestExclusionGuarantees:

    def test_no_excluded_in_any_mapping(self):
        for event_type, raw_triggers in EVENT_ASSET_MAPPING.items():
            for ticker, _, _, _, _, _ in raw_triggers:
                assert ticker not in EXCLUDED_TICKERS, \
                    f"{event_type} contains excluded ticker {ticker}"

    def test_get_triggers_filters_excluded(self):
        # Even if mapping somehow had BTC, get_triggers_for_event must filter it
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert t.ticker not in EXCLUDED_TICKERS

    def test_only_allowed_tickers_in_triggers(self):
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert t.ticker in ALLOWED_TICKERS, \
                    f"{event_type} produced unexpected ticker {t.ticker}"


class TestConvictionBounds:

    def test_all_conviction_in_0_1_range(self):
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert 0.0 <= t.conviction <= 1.0, \
                    f"{event_type}: {t.ticker} conviction={t.conviction} out of range"

    def test_all_directions_valid(self):
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert t.direction in (Direction.LONG, Direction.SHORT, Direction.SKIP), \
                    f"{event_type}: invalid direction {t.direction}"


class TestHalfLife:

    def test_half_life_positive(self):
        for event_type in list_supported_events():
            assert get_max_half_life(event_type) > 0, \
                f"{event_type} has zero half-life"

    def test_half_life_reasonable_bounds(self):
        # Half-lives: 30 min minimum (very fast events), 480 min max (8h, slow macro)
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert 30 <= t.half_life_minutes <= 720, \
                    f"{event_type}: {t.ticker} half_life={t.half_life_minutes} suspicious"


class TestVenueIntegrity:

    def test_only_known_venues(self):
        known_venues = {"Bybit", "T212", "CME", "LBMA", "COMEX"}
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                assert t.venue in known_venues, \
                    f"{event_type}: unknown venue {t.venue}"

    def test_crypto_routes_to_bybit(self):
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                if "USDT" in t.ticker or t.ticker in {"ETH", "SOL"}:
                    assert t.venue == "Bybit", \
                        f"{event_type}: {t.ticker} not routed to Bybit"

    def test_uk_etfs_route_to_t212(self):
        for event_type in list_supported_events():
            triggers = get_triggers_for_event(event_type)
            for t in triggers:
                if t.ticker.endswith(".L"):
                    assert t.venue == "T212", \
                        f"{event_type}: {t.ticker} not routed to T212"


class TestCoverage:

    def test_minimum_event_count(self):
        assert len(list_supported_events()) >= 25

    def test_geopolitics_coverage(self):
        events = list_supported_events()
        # Must have at least one of each major theme
        themes = {
            "hormuz": ["hormuz_easing", "hormuz_escalation"],
            "fed": ["fed_dovish_signal", "fed_hawkish_signal"],
            "ukraine": ["russia_ukraine_escalation", "russia_ukraine_ceasefire"],
            "crypto": ["stablecoin_depeg", "spot_etf_inflow_record"],
        }
        for theme, expected in themes.items():
            for ev in expected:
                assert ev in events, f"Missing {theme} event: {ev}"
