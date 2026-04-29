# DECISION LOG — Commit #004

Append these entries to existing `docs/DECISION_LOG.md`.

---

## D-12 (2026-04-27) — Multi-strategy architecture adopted

**Decision:** Build Orchestra coordinator + 3 new strategies (Mean Reversion,
CVD Divergence, DCA on Macro Events) alongside existing Funding Arb.

**Rationale:**
- Funding arb alone earns 5–8% APR (retail benchmark vs institutional 9.48%)
- Single-strategy bot is fragile to regime changes
- Multi-strategy diversification follows Pythagoras and 1Token Index
  principles: market-neutral funds with multiple uncorrelated alpha sources

**Trade-off:** More code surface, more places for bugs. Mitigated by:
- Abstract BaseStrategy with shared risk gates
- Orchestra-level kill switch
- Mandatory paper-mode validation period

---

## D-13 (2026-04-27) — Reject DP Task #13 backtest numbers

**Decision:** Use DP's strategy LOGIC but ignore reported backtest metrics.

**Rationale:**
- DP claimed "Win Rate 100%" on Multi-Timeframe and ATR Trailing Stop —
  this is mathematically suspicious. 100% win rate over a meaningful sample
  is overfitting, not edge.
- DP CVD Divergence reported -82% max DD with Sharpe 0.78 — a strategy with
  82% drawdown is **not deployable** by definition. Logic worth keeping for
  research, numbers are noise.
- DP Mean Reversion -22% DD might be realistic but is from "49 strategies"
  bucket without methodology disclosure (period, costs, walk-forward).

**Action:** Re-implement the **logic** with our own conservative parameters
defended by institutional benchmarks (1Token Index, ANB Investments).
ChronosBacktester (separate work) will produce honest walk-forward numbers.

---

## D-14 (2026-04-27) — CVD Divergence default DISABLED

**Decision:** Ship CVD Divergence strategy but disable by default.

**Rationale:**
- Underlying signal (smart money distribution) is conceptually valid
- Raw implementation produces large drawdowns (DP backtest: -82%)
- Added 4 filters (BB-width, persistence, RSI≥70, ATR-based stops) but
  filters are not yet validated on out-of-sample data
- Enabling without validation = capital loss

**Re-enable criteria:** ChronosBacktester walk-forward shows
- Sharpe ≥ 1.0 on out-of-sample data
- Max DD ≤ 10%
- Profit factor ≥ 1.3
On all 3 majors (BTC/ETH/SOL).

---

## D-15 (2026-04-27) — Conflict resolution: cancel both on opposite directions

**Decision:** When two strategies signal opposite directions on the same
symbol, Orchestra cancels BOTH signals (does not pick winner).

**Rationale:**
- One of the strategies is wrong. We don't know which.
- Picking by confidence = trusting confidence calibration that hasn't been
  validated across regimes
- Skipping the trade costs nothing in expected value; taking the wrong
  direction costs full position size

**Trade-off:** May skip profitable trades when one strategy is genuinely
right. Acceptable cost for $1K starting capital. Revisit at $10K+ scale.

---

## D-16 (2026-04-27) — Regime matrix locks strategy enable/disable

**Decision:** Hard-coded REGIME_STRATEGY_MATRIX in `orchestra.py` — strategies
not listed for current regime are auto-disabled.

**Current matrix:**
- BULLISH/NEUTRAL: funding_arb + mean_reversion
- VOLATILE: funding_arb + mean_reversion + dca_dips
- BEARISH: funding_arb + dca_dips (no mean_reversion long-only)
- STAGFLATION: funding_arb + dca_dips
- PARABOLIC_BULLISH: funding_arb only (only delta-neutral safe)

**Rationale:** Following Bridgewater All Weather principle — different
strategies for different macro regimes, no single strategy works in all.

**Override:** Manual `/enable_strat` Telegram command bypasses matrix
(emergency or research use only).

---

## D-17 (2026-04-27) — Portfolio-level kill switch at 10% DD

**Decision:** Orchestra halts all strategies if total portfolio DD ≥ 10%.

**Rationale:**
- 1Token institutional Delta Neutral max DD is 0.80%. 10% retail DD is
  already 12x worse — clear sign something is broken.
- Manual /resume required — prevents bot from re-engaging during
  cascading losses

**Cost:** Forced exit during temporary blip = lost potential recovery.
Acceptable — capital preservation > optimization.
