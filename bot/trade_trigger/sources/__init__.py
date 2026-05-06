"""
QA Trade Trigger — News Sources
================================

Active in Phase 3:
  - polymarket.py    Polymarket odds shift detector (leading indicator)

Pending Phase 3-4:
  - mt_newswires.py  (MCP connector — institutional speed)
  - whitehouse.py    (RSS — direct authoritative)
  - state_dept.py    (RSS — diplomatic)
  - ofac.py          (RSS — sanctions)

Each source module exports a *Watcher class with `poll_once()` and
`run_forever(on_event)` methods.
"""

from .polymarket import (
    PolymarketWatcher,
    PolymarketClient,
    MarketSpec,
    DEFAULT_WATCHLIST,
    WatcherConfig as PolymarketConfig,
    OddsShift,
)

__all__ = [
    "PolymarketWatcher",
    "PolymarketClient",
    "MarketSpec",
    "DEFAULT_WATCHLIST",
    "PolymarketConfig",
    "OddsShift",
]
