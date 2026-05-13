"""Status resolution tests (Phase 7.2 Issue 3).

Validates the `_resolve_status` helper from PHASE7_2_INTEGRATION_PATCH.md.
Since the helper is meant to live in scheduler.py (not in this archive),
the test inlines the canonical reference implementation and confirms
the contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Reference implementation (must be identical to what's placed in scheduler.py
# per PHASE7_2_INTEGRATION_PATCH.md — Issue 3 fix)
# ---------------------------------------------------------------------------

def _resolve_status(strat) -> str:
    """Prefer get_status_dict() for accurate /strategies-aligned status."""
    try:
        sd = strat.get_status_dict()
        return sd.get("status", "unknown")
    except Exception:
        return getattr(strat, "status", "unknown")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStatusResolve:

    def test_uses_get_status_dict_when_available(self):
        """FundingArb/LV1 case post-Phase 7.1.3: .status returns DISABLED
        (for orchestra to skip), but get_status_dict()['status'] returns 'PAPER'.
        Resolver must prefer the latter."""
        strat = MagicMock()
        strat.get_status_dict.return_value = {"status": "PAPER", "name": "funding_arb_v1"}
        strat.status = "DISABLED"   # intentional dissonance per Phase 7.1.3

        result = _resolve_status(strat)
        assert result == "PAPER"

    def test_falls_back_when_get_status_dict_missing(self):
        """Old strategies without get_status_dict() fall back to .status."""
        strat = MagicMock(spec=["status"])  # only `status` attribute exists
        strat.status = "PAPER"

        result = _resolve_status(strat)
        assert result == "PAPER"

    def test_falls_back_when_get_status_dict_raises(self):
        """Defensive: get_status_dict() exception → fall back to .status."""
        strat = MagicMock()
        strat.get_status_dict.side_effect = RuntimeError("snapshot mid-flight")
        strat.status = "ACTIVE"

        result = _resolve_status(strat)
        assert result == "ACTIVE"

    def test_returns_unknown_if_both_fail(self):
        """No get_status_dict, no .status → 'unknown'."""
        # spec=[] gives only the methods we don't list — neither status nor
        # get_status_dict are mockable, so both AttributeError
        strat = MagicMock(spec=[])

        result = _resolve_status(strat)
        assert result == "unknown"

    def test_get_status_dict_returns_dict_without_status_key(self):
        """If returned dict lacks 'status', default to 'unknown' (not fall through to .status)."""
        strat = MagicMock()
        strat.get_status_dict.return_value = {"name": "foo"}   # no 'status'
        strat.status = "ACTIVE"

        result = _resolve_status(strat)
        # Per the .get(..., "unknown") default, we return unknown — NOT fall through
        assert result == "unknown"
