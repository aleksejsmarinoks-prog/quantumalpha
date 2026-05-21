"""RegimeDetector tests (Phase 6.3.1a Step 2).

Critical focus: anti-lookahead invariant. 4 dedicated tests verify
detect_at(t) only uses bars with timestamp STRICTLY < t.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, RegimeDetector, RegimeConfig, make_regime_provider,
    ReplayEngineV2, OpenAction, REGIME_LOW, REGIME_NORMAL, REGIME_HIGH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc
T0 = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)


def _bar(idx: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Bar:
    return Bar(
        timestamp=T0 + timedelta(minutes=5 * idx),
        open=o, high=h, low=l, close=c, volume=v,
    )


def _flat_bars(n: int, price: float = 2000.0, spread: float = 1.0) -> List[Bar]:
    """n bars at ~price with tiny spread — yields LOW_VOL."""
    return [_bar(i, price, price + spread, price - spread, price) for i in range(n)]


def _volatile_bars(n: int, base: float = 2000.0, spread: float = 60.0) -> List[Bar]:
    """n bars with wide ranges — yields HIGH_VOL.
    spread=60 on price=2000 → 3% range → ATR ~3% → HIGH_VOL.
    """
    bars = []
    price = base
    for i in range(n):
        h = price + spread
        l = price - spread
        # alternate up/down candles
        c = h if i % 2 == 0 else l
        bars.append(_bar(i, price, h, l, c))
        price = c
    return bars


# ===========================================================================
# Basic detection
# ===========================================================================

class TestBasicDetection:

    def test_low_vol_market(self):
        bars = _flat_bars(50)
        detector = RegimeDetector(bars)
        # Detect at last bar's timestamp + 1 minute (so all 50 bars are visible)
        ts = bars[-1].timestamp + timedelta(minutes=1)
        regime = detector.detect_at(ts)
        assert regime == REGIME_LOW

    def test_high_vol_market(self):
        bars = _volatile_bars(50)
        detector = RegimeDetector(bars)
        ts = bars[-1].timestamp + timedelta(minutes=1)
        regime = detector.detect_at(ts)
        assert regime == REGIME_HIGH

    def test_normal_vol_market(self):
        # ATR ~1% on price 2000 → in normal band (0.5-1.5%)
        bars = []
        price = 2000.0
        for i in range(50):
            spread = 12.0   # ~0.6% half-range → ATR ~1%
            h = price + spread
            l = price - spread
            c = price + (spread * 0.3 if i % 2 == 0 else -spread * 0.3)
            bars.append(_bar(i, price, h, l, c))
            price = c
        detector = RegimeDetector(bars)
        ts = bars[-1].timestamp + timedelta(minutes=1)
        regime = detector.detect_at(ts)
        assert regime == REGIME_NORMAL


# ===========================================================================
# Anti-lookahead invariant (THE critical property)
# ===========================================================================

class TestAntiLookahead:

    def test_strictly_before_only_excludes_current_bar(self):
        """detect_at(bar_N.timestamp) must NOT include bar_N itself."""
        # 20 flat bars + 1 wild bar at the END
        flat = _flat_bars(20)
        wild_bar = Bar(
            timestamp=flat[-1].timestamp + timedelta(minutes=5),
            open=2000, high=2500, low=1500, close=2000,   # 25% intra-bar range
            volume=1000,
        )
        bars = flat + [wild_bar]
        detector = RegimeDetector(bars)

        # detect AT the wild bar timestamp → must NOT see the wild bar
        regime_at_wild = detector.detect_at(wild_bar.timestamp)
        assert regime_at_wild == REGIME_LOW, (
            "Regime AT wild bar timestamp must use ONLY pre-wild bars, "
            "which are all flat → must be LOW_VOL"
        )

    def test_one_second_after_bar_includes_it(self):
        """detect_at(bar_N.timestamp + 1µs) DOES see bar_N (bar is strictly before)."""
        # 14 flat bars + 1 wild bar
        flat = _flat_bars(14)
        wild_bar = Bar(
            timestamp=flat[-1].timestamp + timedelta(minutes=5),
            open=2000, high=2500, low=1500, close=2000,
            volume=1000,
        )
        bars = flat + [wild_bar]
        detector = RegimeDetector(bars, config=RegimeConfig(atr_period=14))

        # 1 microsecond after wild bar → wild bar IS strictly before
        regime_after = detector.detect_at(wild_bar.timestamp + timedelta(microseconds=1))
        # ATR including the wild bar (25% range) should now be HIGH_VOL or NORMAL
        assert regime_after in (REGIME_HIGH, REGIME_NORMAL)

    def test_query_before_any_bar(self):
        """Query with timestamp before all bars → None (no history)."""
        bars = _flat_bars(50)
        detector = RegimeDetector(bars)
        regime = detector.detect_at(T0 - timedelta(hours=1))
        assert regime is None

    def test_insufficient_history_returns_none(self):
        """Need atr_period+1 bars before timestamp."""
        bars = _flat_bars(50)
        detector = RegimeDetector(bars, config=RegimeConfig(atr_period=14))
        # Query at bar 10's timestamp — only 10 bars STRICTLY before → < 14+1
        ts = bars[10].timestamp
        regime = detector.detect_at(ts)
        assert regime is None

    def test_exactly_enough_history(self):
        """atr_period+1 bars STRICTLY before → first valid query."""
        bars = _flat_bars(50)
        detector = RegimeDetector(bars, config=RegimeConfig(atr_period=14))
        # Query at bar 15's timestamp — 15 bars strictly before → 15 >= 14+1
        ts = bars[15].timestamp
        regime = detector.detect_at(ts)
        assert regime is not None
        assert regime == REGIME_LOW


# ===========================================================================
# Config + thresholds
# ===========================================================================

class TestConfig:

    def test_custom_thresholds(self):
        """Tighter bands → flat market reclassified as NORMAL."""
        bars = _flat_bars(50)
        # Set low_threshold absurdly tight (0.001%) → flat market not low anymore
        config = RegimeConfig(low_threshold_pct=0.001, high_threshold_pct=10.0)
        detector = RegimeDetector(bars, config=config)
        ts = bars[-1].timestamp + timedelta(minutes=1)
        regime = detector.detect_at(ts)
        # ATR/close on flat bars > 0.001% almost certainly → NORMAL
        assert regime != REGIME_LOW

    def test_invalid_config_thresholds(self):
        with pytest.raises(ValueError, match="must be <"):
            RegimeConfig(low_threshold_pct=2.0, high_threshold_pct=1.0)

    def test_invalid_atr_period(self):
        with pytest.raises(ValueError, match="atr_period"):
            RegimeConfig(atr_period=1)


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:

    def test_empty_bars_raises(self):
        with pytest.raises(ValueError, match="at least 1 bar"):
            RegimeDetector([])

    def test_caching(self):
        bars = _flat_bars(50)
        detector = RegimeDetector(bars)
        ts = bars[-1].timestamp + timedelta(minutes=1)
        r1 = detector.detect_at(ts)
        r2 = detector.detect_at(ts)
        # Same result, cache hit (cache populated)
        assert r1 == r2
        assert ts in detector._cache

    def test_helpers_atr_and_ratio(self):
        bars = _flat_bars(50)
        detector = RegimeDetector(bars)
        ts = bars[-1].timestamp + timedelta(minutes=1)
        atr = detector.get_atr_at(ts)
        ratio = detector.get_ratio_pct_at(ts)
        assert atr is not None and atr > 0
        assert ratio is not None and 0 < ratio < 1.0   # flat market

    def test_unsorted_input_is_sorted(self):
        """Constructor accepts unsorted bars and sorts internally."""
        bars = _flat_bars(20)
        shuffled = bars[10:] + bars[:10]   # rotate
        detector = RegimeDetector(shuffled)
        # Internal _timestamps must be sorted
        assert detector._timestamps == sorted(detector._timestamps)


# ===========================================================================
# Integration with ReplayEngineV2 (Step 1)
# ===========================================================================

class TestIntegrationWithEngine:

    def test_regime_appears_in_snapshot(self):
        """Engine pulls regime from provider; adapter sees it in snapshot."""
        bars = _flat_bars(50)
        provider = make_regime_provider(bars)

        seen_regimes = []

        class SnapshotCollector:
            def evaluate(self, snapshot, positions):
                seen_regimes.append(snapshot.regime)
                return []

        eng = ReplayEngineV2(
            symbol="ETHUSDT",
            initial_equity=1000.0,
            regime_provider=provider,
        )
        eng.run(bars, SnapshotCollector())

        # First ~15 bars don't have enough history → regime=None
        assert seen_regimes[0] is None
        # By bar 20+ regime should be populated
        late_regimes = [r for r in seen_regimes[20:] if r is not None]
        assert len(late_regimes) > 0
        # All flat → LOW_VOL
        assert all(r == REGIME_LOW for r in late_regimes)

    def test_factory_function(self):
        """make_regime_provider returns working callable."""
        bars = _flat_bars(50)
        provider = make_regime_provider(bars)
        assert callable(provider)
        # Call directly
        ts = bars[-1].timestamp + timedelta(minutes=1)
        regime = provider(ts)
        assert regime == REGIME_LOW
