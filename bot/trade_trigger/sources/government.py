"""
QA Trade Trigger — Government RSS Sources
===========================================

Direct authoritative sources. Tier-1, bypass corroboration gate.

  WhiteHouseWatcher — White House press releases
  OFACWatcher       — Treasury OFAC sanctions actions (real-time crypto impact)
  StateDeptWatcher  — State Department announcements

These complement Polymarket (leading indicator) with primary-source events.
WH/OFAC/State are listed in CorroborationGate.config.direct_source_domains
so a single event from these passes the gate without needing a second source.

Author: QuantumAlpha
Version: 0.4.0
"""

from __future__ import annotations

from ..models import Tier
from .rss_base import RSSWatcherBase, RSSWatcherConfig


# ---------------------------------------------------------------------------
# White House
# ---------------------------------------------------------------------------

class WhiteHouseWatcher(RSSWatcherBase):
    """White House press releases — primary source for Trump statements,
    executive orders, policy announcements."""
    feed_url = "https://www.whitehouse.gov/feed/"
    source_domain = "whitehouse.gov"
    source_tier = Tier.T1
    source_name = "whitehouse"


# ---------------------------------------------------------------------------
# OFAC (Treasury sanctions)
# ---------------------------------------------------------------------------

class OFACWatcher(RSSWatcherBase):
    """OFAC recent actions — sanctions designations.
    Real-time crypto-impact source: SDN list updates affect ETH/SOL liquidity
    via stablecoin freezes and exchange compliance actions."""
    feed_url = "https://ofac.treasury.gov/system/files/126/recent_actions.rss"
    source_domain = "ofac.treasury.gov"
    source_tier = Tier.T1
    source_name = "ofac"


# ---------------------------------------------------------------------------
# State Department
# ---------------------------------------------------------------------------

class StateDeptWatcher(RSSWatcherBase):
    """State Department press releases and briefings.
    Diplomatic announcements (Iran negotiations, NATO statements, ceasefires)."""
    feed_url = "https://www.state.gov/press-releases/feed/"
    source_domain = "state.gov"
    source_tier = Tier.T1
    source_name = "state_dept"


# ---------------------------------------------------------------------------
# Federal Reserve press releases (bonus — Fed-related events)
# ---------------------------------------------------------------------------

class FedWatcher(RSSWatcherBase):
    """Federal Reserve press releases — FOMC statements, speeches, policy.
    Critical for catching Powell/Warsh signals before financial press picks up."""
    feed_url = "https://www.federalreserve.gov/feeds/press_all.xml"
    source_domain = "federalreserve.gov"
    source_tier = Tier.T1
    source_name = "fed"


# ---------------------------------------------------------------------------
# Factory — convenience for bot_runner
# ---------------------------------------------------------------------------

def all_government_watchers(db, config: RSSWatcherConfig = None):
    """Return list of all four government RSS watchers, ready to poll."""
    cfg = config or RSSWatcherConfig()
    return [
        WhiteHouseWatcher(db, config=cfg),
        OFACWatcher(db, config=cfg),
        StateDeptWatcher(db, config=cfg),
        FedWatcher(db, config=cfg),
    ]
