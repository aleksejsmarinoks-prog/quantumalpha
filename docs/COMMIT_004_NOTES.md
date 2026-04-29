# Commit #004 — Multi-Strategy Bot Architecture

**Date:** 2026-04-27
**LOC added:** ~3,200
**Status:** ✅ Code complete, ✅ integration tests passing, ⏳ awaiting VPS deploy

## What's new

### New strategies (3)

1. **`bot/strategies/mean_reversion.py`** — *Panic Buyer*
   - Buy spot BTC/ETH/SOL on -5% 1h or RSI<25 panic
   - 3-tier scale-in (30/30/40%)
   - Hard absolute stop -15%
   - **Default: ENABLED in paper-mode**

2. **`bot/strategies/cvd_divergence.py`** — *Smart Money Fade*
   - Short on price-up + CVD-down divergence
   - Mandatory RSI>70, BB-width>50pctile, persistence≥2h
   - 2x ATR hard stop, 4x ATR take profit
   - **Default: DISABLED** — needs walk-forward validation before enabling
   - Reason: DP backtest claimed -82% DD on raw logic; adding filters but
     not yet validated

3. **`bot/strategies/dca_dips.py`** — *Chaos Accumulator*
   - Triggered by external macro events (VIX>30, Fed, geopolitical CRITICAL)
   - 5 tranches over 30h, asset weights 50/30/20 BTC/ETH/SOL
   - Per-event hard stop -8%, trailing stop -3% after profit
   - 7-day cooldown between events
   - **Default: ENABLED in paper-mode**

### Coordination layer

4. **`bot/strategies/base_strategy.py`** — Abstract base class
   - Unified Signal contract (HOLD/ENTER_LONG/ENTER_SHORT/SCALE_IN/EXIT)
   - Shared risk gates (cooldowns, bearish-block, daily-loss limit)
   - Status state machine (DISABLED/PAPER/LIVE/HALTED/COOLDOWN)

5. **`bot/strategies/orchestra.py`** — *Strategy Orchestra*
   - Multi-strategy coordinator
   - Capital allocation per strategy via config.capital_pct
   - Conflict resolution: opposite-direction → cancel both (safety)
   - Same-direction → highest confidence wins
   - Regime-based strategy enable/disable matrix
   - Portfolio-level kill switch (10% DD halts all)
   - Per-symbol exposure cap (20% of total)

### Macro layer

6. **`bot/core/macro_events.py`** — *Macro Event Detector*
   - Async polling: VIX (yfinance), FOMC dates, geopolitical manual
   - Hysteresis on VIX trigger (30/22 thresholds)
   - 30-min sustained-above-threshold check (no flickering)
   - 24h re-fire cooldown
   - 8 known FOMC 2026 dates pre-loaded

### Infrastructure

7. **`bot/handlers/strategy_commands.py`** — Telegram strategy commands
   - `/strategies` — list all strategies + status
   - `/strat_status <id>` — detailed JSON status
   - `/enable_strat <id> [paper|live]` — enable
   - `/disable_strat <id>` — disable
   - `/halt_strat <id>` — emergency halt
   - `/resume_strat <id>` — resume
   - `/strat_positions` — all active positions
   - `/orchestra` — orchestra portfolio metrics

8. **Updated `bot/main.py`** — Wires Orchestra + macro detector
   - Reads `QA_MODE=paper|live` env var (default paper)
   - Reads `QA_TOTAL_CAPITAL_USD` env var (default 1000)
   - Sends Telegram startup notification

9. **Updated `bot/scheduler.py`** — 3 new background jobs
   - `orchestra_tick` — every 5 min, runs multi-strategy evaluate+execute
   - `portfolio_value_sync` — every 10 min, updates orchestra DD tracking
   - Existing 5 jobs from commit #003 preserved

## Empirical grounding

All strategy parameters defended against 1Token Institutional 2025 Index Report:

- **Delta Neutral on Bybit (institutional): 9.48% APR, 0.80% max DD**
  → our funding_arb retail target: 5–8% APR, 2% max DD
- **Mean Reversion crypto majors:** ANB Investments principle, "same model
  on all 3 majors" (BTC/ETH/SOL) to mitigate overfitting
  → our parameters fixed once, no per-asset tuning
- **DCA on macro spikes:** academic studies show median +5%, range -8% to +20%
  per event, with proper risk caps

## Honest limitations

- **CVD Divergence is risky.** DP Task #13 reported -82% DD on raw logic.
  We added 4 filters but have NOT validated them. Default: DISABLED.
- **No backtests in this codebase.** Backtesting belongs in
  ChronosBacktester (not yet built). Walk-forward validation required
  before any LIVE enabling.
- **Paper-mode execution simulates fills at trigger price.** Real slippage
  (especially during panic candles) will be worse. Reserve $50 of $1000
  paper capital as buffer.
- **Macro detector requires `yfinance`.** Add to requirements.txt.

## Open work

- [ ] Funding Arb adapter — current funding_arb.py from commit #002 doesn't
      yet inherit BaseStrategy. Needs ~50-line adapter or re-write.
- [ ] CVD data feed — needs tick-by-tick `/v5/market/recent-trade` polling
      and tick-test classification (buyer vs seller initiated)
- [ ] xStocks integration — separate module if/when we add equity rotation
- [ ] ChronosBacktester walk-forward harness — parallel work
- [ ] DAC8 tax accounting hooks — coordinate with bookkeeper

## Deployment readiness

| Component | Status |
|---|---|
| Code | ✅ Written, integration-tested |
| Imports | ✅ All resolve cleanly |
| Logic | ✅ 5 integration tests pass |
| Paper mode | ✅ Default mode |
| Kill switch | ✅ Tested |
| Conflict resolution | ✅ Tested |
| Bearish-regime block | ✅ Tested |
| VPS deploy | ⏳ Awaits Hetzner registration |
| Live trading | ⏳ Awaits 14d baseline + 30d paper |
| CVD divergence | ⛔ Default disabled until validated |

## Next steps after VPS deploy

1. Days 0–14: collect baseline funding rates + market data, paper-mode runs
2. Day 14: ChronosBacktester recalibration on collected data
3. Days 14–44: paper-mode strategy validation (target Sharpe ≥ 1.5, DD ≤ 8%)
4. Day 44+: enable LIVE for funding_arb_v1 first, $200/leg
5. Day 60+: enable mean_reversion_v1 in LIVE if paper Sharpe ≥ 1.0
6. Day 90+: review CVD divergence backtest results, decide enable/keep-disabled

## Telegram quick start (after deploy)

```
/strategies            # see all
/orchestra             # portfolio status
/strat_status mean_reversion_v1
/disable_strat cvd_divergence_v1   # already disabled but explicit is ok
```
