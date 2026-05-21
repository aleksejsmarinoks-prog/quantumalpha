"""
QA Backtest — RealMeanReversionAdapter (Phase 6.3.1b-B)
=========================================================

Connects production `MeanReversionStrategy` to walk-forward harness.

Inherits all wiring from `MeanReversionAdapter` (Step 5a):
  - `prepare_market_data` pulls `last_price`, `returns_1h`, `rsi_14_1h` from
    `IndicatorsProvider` output dict (Phase 6.3.1b-A Q6.1 added `close_1h_history`)
  - Signal type translation via `_signal_type_value()` (.lower() normalized so
    production `SignalType.EXIT.value == "EXIT"` matches `PROD_EXIT == "exit"`)
  - Per-tick `set_strategy_capital()` sync (Phase 6.3.1b-A Q6.3 fix)
  - `now=snapshot.timestamp` injection in `evaluate()` (Phase 6.3.1b-B Bug-1 fix)
  - `on_tier_filled` / `on_position_closed` callbacks already use
    `_call_with_optional_now` (Step 3 patches)

Usage:
    from bot.backtest.adapters.real_mean_reversion_adapter import RealMeanReversionAdapter
    from bot.backtest.regime_detector import make_trend_regime_provider

    adapter = RealMeanReversionAdapter(starting_capital_usd=200.0)
    regime_provider = make_trend_regime_provider(bars)
    # ... use with WalkForwardHarness

Note: pair with `make_trend_regime_provider`, NOT `make_regime_provider` — production
`MeanReversionStrategy.apply_risk_gates` checks `regime == "BEARISH"` (trend),
not vol-based regime strings.

Author: QuantumAlpha
Phase: 6.3.1b-B
"""

from __future__ import annotations

from bot.backtest.adapters.mean_reversion_adapter import MeanReversionAdapter

# Production strategy import — must be available on VPS (deployed at commit f54c562).
# In local backtest sandbox where bot.strategies module may not exist,
# this import will fail at module load — that's intentional (driver fails fast
# when production strategy isn't present, rather than silently running mock).
try:
    from bot.strategies.mean_reversion import MeanReversionStrategy
    _PRODUCTION_STRATEGY_AVAILABLE = True
except ImportError as e:
    MeanReversionStrategy = None    # type: ignore[assignment,misc]
    _PRODUCTION_STRATEGY_AVAILABLE = False
    _IMPORT_ERROR = e


class RealMeanReversionAdapter(MeanReversionAdapter):
    """Walk-forward adapter wrapping production MeanReversionStrategy.

    All wiring inherited from MeanReversionAdapter (Step 5a) — only swaps
    `strategy_class` to point at production code.
    """
    strategy_class = MeanReversionStrategy

    def __init__(self, *args, **kwargs):
        if not _PRODUCTION_STRATEGY_AVAILABLE:
            raise ImportError(
                "Production `bot.strategies.mean_reversion.MeanReversionStrategy` "
                f"not available: {_IMPORT_ERROR}. "
                "Cannot construct RealMeanReversionAdapter without it. "
                "Verify VPS deploy has production strategies (Step 3 commit f54c562 + later)."
            )
        super().__init__(*args, **kwargs)
