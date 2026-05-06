"""BybitProvider adapter tests — covers multiple API shapes."""

from __future__ import annotations

import pytest

from bot.trade_trigger.core_adapters.bybit_provider import BybitProvider


# ---------------------------------------------------------------------------
# Mock BybitClient variants
# ---------------------------------------------------------------------------

class BybitV5StyleClient:
    """Real Bybit V5 shape: list of [start, open, high, low, close, vol, turnover]."""
    def __init__(self):
        self.calls = []

    async def get_klines(self, symbol, interval, limit):
        self.calls.append((symbol, interval, limit))
        # Newest-first (Bybit's actual behaviour)
        # ts in ms, close as string
        return [
            ["1715000000000", "2050.0", "2055", "2049", "2052.5", "100", "1"],
            ["1714996400000", "2048.0", "2052", "2046", "2050.0", "120", "1"],
            ["1714992800000", "2045.0", "2050", "2044", "2048.0", "110", "1"],
        ]


class DictEnvelopeClient:
    """API returning {result: {list: [...]}} envelope."""
    async def get_klines(self, symbol, interval, limit):
        return {
            "result": {
                "list": [
                    ["1715000000000", "2050", "2055", "2049", "2052.5", "100", "1"],
                    ["1714996400000", "2048", "2052", "2046", "2050.0", "120", "1"],
                ]
            }
        }


class KwargsOnlyClient:
    """Client that requires keyword args."""
    async def fetch_klines(self, *, symbol, interval, limit):
        return [
            [1715000000000, 2050.0, 2055, 2049, 2052.5, 100],
            [1714996400000, 2048.0, 2052, 2046, 2050.0, 120],
        ]


class DictListClient:
    """Returns list of dicts with 'close' field."""
    async def get_klines(self, symbol, interval, limit):
        return [
            {"start": 1715000000000, "close": "2052.5"},
            {"start": 1714996400000, "close": "2050.0"},
            {"start": 1714992800000, "close": "2048.0"},
        ]


class BrokenClient:
    """Always raises."""
    async def get_klines(self, *args, **kwargs):
        raise ConnectionError("bybit unreachable")


class EmptyClient:
    """Returns empty list."""
    async def get_klines(self, *args, **kwargs):
        return []


class NoMethodClient:
    """Has no recognized method."""
    async def something_else(self): pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBybitProvider:

    async def test_v5_shape_works(self):
        client = BybitV5StyleClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETH/USDT", count=3)
        assert closes is not None
        assert len(closes) == 3
        # Should be reversed to oldest-first
        assert closes[0] < closes[-1]   # oldest cheapest, newest most expensive
        assert closes[-1] == pytest.approx(2052.5)

    async def test_symbol_normalized(self):
        client = BybitV5StyleClient()
        provider = BybitProvider(client)
        await provider.get_klines_1h("ETH/USDT", count=3)
        # Verify normalization happened: symbol passed without slash
        assert client.calls[0][0] == "ETHUSDT"

    async def test_dict_envelope_unwrapped(self):
        client = DictEnvelopeClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETHUSDT", count=2)
        assert closes is not None
        assert len(closes) == 2

    async def test_kwargs_only_signature(self):
        client = KwargsOnlyClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETHUSDT", count=2)
        assert closes is not None
        assert len(closes) == 2

    async def test_dict_list_with_close_field(self):
        client = DictListClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETHUSDT", count=3)
        assert closes is not None
        assert len(closes) == 3

    async def test_broken_client_returns_none(self):
        client = BrokenClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETHUSDT", count=10)
        assert closes is None

    async def test_empty_response_returns_none(self):
        client = EmptyClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETHUSDT", count=10)
        assert closes is None

    async def test_no_method_returns_none(self):
        client = NoMethodClient()
        provider = BybitProvider(client)
        closes = await provider.get_klines_1h("ETHUSDT", count=10)
        assert closes is None

    async def test_method_caching(self):
        """After first success, method should be cached."""
        client = BybitV5StyleClient()
        provider = BybitProvider(client)
        assert provider._method_name is None
        await provider.get_klines_1h("ETHUSDT", count=2)
        assert provider._method_name == "get_klines"


class TestExtractCloses:
    """Direct test of _extract_closes static helper."""

    def test_v5_shape(self):
        raw = [
            [0, 100, 110, 90, 105, 1000, 0],
            [1, 95, 105, 85, 100, 1000, 0],
        ]
        closes = BybitProvider._extract_closes(raw)
        assert closes is not None

    def test_envelope_shape(self):
        raw = {"result": {"list": [[0, 100, 110, 90, 105, 1000, 0]]}}
        closes = BybitProvider._extract_closes(raw)
        assert closes == [105.0]

    def test_none_returns_none(self):
        assert BybitProvider._extract_closes(None) is None

    def test_empty_list(self):
        assert BybitProvider._extract_closes([]) is None

    def test_garbage_returns_none(self):
        assert BybitProvider._extract_closes("not a list") is None

    def test_dict_with_close_field(self):
        raw = [{"close": "100.5"}, {"close": "101.0"}]
        closes = BybitProvider._extract_closes(raw)
        assert closes is not None
        assert len(closes) == 2
