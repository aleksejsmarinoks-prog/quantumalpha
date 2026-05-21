"""Pytest fixtures for ReplayEngine v2 tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.backtest import (
    Bar, SnapshotContext, Position, OpenAction, ScaleInAction,
    ReduceAction, CloseAction, TakeProfit,
)


@pytest.fixture
def utc_now():
    return datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)


def _bar(ts, o, h, l, c, v=1000.0) -> Bar:
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


@pytest.fixture
def flat_bars(utc_now) -> List[Bar]:
    """5 bars, all at price 2000 (no movement). For testing pure action flow."""
    return [
        _bar(utc_now + timedelta(minutes=5 * i), 2000, 2001, 1999, 2000)
        for i in range(5)
    ]


@pytest.fixture
def trending_up_bars(utc_now) -> List[Bar]:
    """10 bars trending up from 2000 → 2100 by +10/bar."""
    bars = []
    price = 2000.0
    for i in range(10):
        bars.append(_bar(
            utc_now + timedelta(minutes=5 * i),
            o=price, h=price + 8, l=price - 2, c=price + 10,
        ))
        price += 10
    return bars


@pytest.fixture
def crash_bars(utc_now) -> List[Bar]:
    """5 bars where bar 2 crashes from 2000 → 1900."""
    return [
        _bar(utc_now + timedelta(minutes=0),  2000, 2001, 1999, 2000),
        _bar(utc_now + timedelta(minutes=5),  2000, 2002, 1998, 2001),
        _bar(utc_now + timedelta(minutes=10), 2001, 2002, 1900, 1910),   # crash
        _bar(utc_now + timedelta(minutes=15), 1910, 1920, 1900, 1915),
        _bar(utc_now + timedelta(minutes=20), 1915, 1925, 1905, 1920),
    ]


@pytest.fixture
def spike_up_bars(utc_now) -> List[Bar]:
    """5 bars where bar 2 spikes high (tests take-profit triggers)."""
    return [
        _bar(utc_now + timedelta(minutes=0),  2000, 2001, 1999, 2000),
        _bar(utc_now + timedelta(minutes=5),  2000, 2002, 1998, 2001),
        _bar(utc_now + timedelta(minutes=10), 2001, 2100, 2000, 2050),   # spike to 2100
        _bar(utc_now + timedelta(minutes=15), 2050, 2055, 2045, 2050),
        _bar(utc_now + timedelta(minutes=20), 2050, 2052, 2048, 2050),
    ]


# ---------------------------------------------------------------------------
# Sample adapters (test doubles)
# ---------------------------------------------------------------------------

class OpenAtBarAdapter:
    """Opens LONG at bar `open_at_index` only. Tracks callbacks."""
    def __init__(self, open_at_index=0, qty=0.1, side="LONG",
                 stop_loss=None, take_profits=(), close_at_index=None):
        self.open_at_index = open_at_index
        self.qty = qty
        self.side = side
        self.stop_loss = stop_loss
        self.take_profits = take_profits
        self.close_at_index = close_at_index
        self._count = 0
        self.fills_received = []
        self.closes_received = []
        self.snapshots_seen = []

    def evaluate(self, snapshot, positions):
        self.snapshots_seen.append(snapshot)
        actions = []
        if self._count == self.open_at_index:
            actions.append(OpenAction(
                symbol=snapshot.symbol,
                side=self.side,
                qty=self.qty,
                stop_loss=self.stop_loss,
                take_profits=tuple(self.take_profits),
            ))
        if self.close_at_index is not None and self._count == self.close_at_index:
            for p in positions:
                actions.append(CloseAction(position_id=p.id))
        self._count += 1
        return actions

    def on_fill(self, position, fill):
        self.fills_received.append((position.id, fill.price, fill.qty))

    def on_position_closed(self, position):
        self.closes_received.append((position.id, position.close_reason,
                                     position.realized_pnl))


class ScaleInAdapter:
    """Opens at bar 0, scales in qty=0.05 at bar 2, closes at bar 4."""
    def __init__(self):
        self._count = 0
        self.fills_received = []

    def evaluate(self, snapshot, positions):
        actions = []
        if self._count == 0:
            actions.append(OpenAction(symbol=snapshot.symbol, side="LONG", qty=0.1))
        elif self._count == 2 and positions:
            actions.append(ScaleInAction(position_id=positions[0].id, qty=0.05))
        elif self._count == 4 and positions:
            actions.append(CloseAction(position_id=positions[0].id))
        self._count += 1
        return actions

    def on_fill(self, position, fill):
        self.fills_received.append(fill.qty)
