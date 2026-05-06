"""
QA Trade Trigger — News Sources
================================

Active in Phase 4:
  - polymarket.py         Polymarket odds shift detector (leading indicator)
  - government.py         WhiteHouse, OFAC, State Dept, Fed RSS feeds
  - rss_base.py           Common RSS scaffolding

Pending Phase 5+:
  - mt_newswires.py       MCP connector (institutional speed)
  - twitter_x.py          Direct X API (Trump posts, breaking news)
"""

from .polymarket import (
    PolymarketWatcher,
    PolymarketClient,
    MarketSpec,
    DEFAULT_WATCHLIST,
    WatcherConfig as PolymarketConfig,
    OddsShift,
)
from .rss_base import RSSWatcherBase, RSSWatcherConfig
from .government import (
    WhiteHouseWatcher,
    OFACWatcher,
    StateDeptWatcher,
    FedWatcher,
    all_government_watchers,
)

__all__ = [
    # Polymarket
    "PolymarketWatcher", "PolymarketClient", "MarketSpec",
    "DEFAULT_WATCHLIST", "PolymarketConfig", "OddsShift",
    # RSS base
    "RSSWatcherBase", "RSSWatcherConfig",
    # Government
    "WhiteHouseWatcher", "OFACWatcher", "StateDeptWatcher", "FedWatcher",
    "all_government_watchers",
]
