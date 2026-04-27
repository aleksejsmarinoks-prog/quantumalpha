# Decision Log

Every architectural decision recorded here. Format:

```
## [Date] [Title]
**Context:** Why this needed deciding
**Decision:** What we chose
**Alternatives:** What we rejected and why
**Status:** Active | Superseded | Deprecated
```

---

## 2026-04-27 — Project repositioning: prop trading first

**Context:** Original QA project focused on macro pipeline + signal distribution.
After due diligence review (DeepSeek adversarial DD), commercial monetization
gates (live track record, MiFID compliance) were identified as 6+ months out.
User refocused project on prop trading first, monetization deferred until
3-6 months stable PnL is demonstrated.

**Decision:**
- Prop trading bot becomes Phase 1 priority.
- Macro QA pipeline retained as **regime filter** for QuantForge, not center of system.
- Capital allocation:
  - $1,000 active trading (validation)
  - $25,000 passive Earn (anti-inflation, 7-9% APR)
  - $37,500 strategic hold (USDT, available for scale-up)
- Commercial monetization deferred to month 4+ at earliest.

**Status:** Active.

---

## 2026-04-27 — Strategy implementation order

**Decision:**
1. **P1 (week 1-3):** Funding rate arbitrage (delta-neutral)
2. **P1 (week 4-7):** Basis trade (calendar spread)
3. **P2 (week 5-8):** Mean reversion on panic dumps
4. **P3 (after 6 months live):** Scalping/momentum (only if P1+P2 stable)
5. **NEVER:** Martingale, grid without stop-out, DCA without max position cap

**Status:** Active.

---

## 2026-04-27 — Risk Kernel design: hard caps over optimization

**Decision:** Centralized Risk Kernel with VETO power over all trades.
**Hard caps:** Max position 25% of equity, max leverage 3x, daily DD 5%, weekly DD 10%, total DD 15%, 3 consecutive losses → 24h cooldown.

**Status:** Active. Implemented in `bot/core/risk_kernel.py`.

---

## 2026-04-27 — PnL Ledger: SQLite, append-only, tax-grade

**Decision:** SQLite WAL with 7 tables. Idempotent on `fill_id`. Tax-grade audit trail.

**Status:** Active. Implemented in `bot/core/pnl_ledger.py`.

---

## 2026-04-27 — Bybit Earn API: read-only initially, upgraded to optional auto-mode after Task #8

**Original decision (pre-Task #8):** No automatic Earn subscription. User
subscribes manually via UI, records to bot via `/earn_add` command.

**Updated 2026-04-27 (post DeepSeek Task #8 verification):**
- `/v5/earn/place-order` and `/v5/earn/product` confirmed available.
- EarnManager v1.1 supports BOTH read-only AND live auto-subscription mode.
- Live mode gated behind `LIVE_EARN_MODE=true` env flag + Bybit API keys.
- Default: read-only (safe).
- Only `flexible_savings` and `onchain_staking` are automatable.
- `fixed_term`, `dual_asset`, `discount_buy`, `launchpool` remain manual UI.

**Source:** DeepSeek Task #8, Bybit V5 docs at
https://bybit-exchange.github.io/docs/v5/finance/earn/easy-onchain

**Status:** Active. Implemented in `bot/core/earn_manager.py` v1.1.

---

## 2026-04-27 — Funding Monitor: separate DB for market data

**Decision:** Funding rates stored in separate `data/funding_history.db`,
NOT in pnl.db. Includes:
- `funding_rate_history` table — every snapshot, dedupes per settlement
- `funding_opportunities` table — events when threshold crossed

**Rationale:** Cleaner schema separation. PnL ledger remains tax-focused.
Market data can be analyzed/exported independently.

**Status:** Active. Implemented in `bot/core/funding_monitor.py`.

---

## 2026-04-27 — Funding Arb thresholds: per-symbol, conservative override of DeepSeek recommendation

**Context:** DeepSeek Task #9 recommended open=0.028%/8h based on a 3-day
break-even calculation. This is **literally at break-even** — zero expected
edge, all variance is noise.

**Decision:**
- Override DP recommendation. Open threshold raised to **0.040%/8h** for ETH
  (1.5x break-even), and **0.050%/8h** for SOL (accounts for higher slippage).
- Close threshold: 0.012%/8h (ETH), 0.015%/8h (SOL).
- Per-symbol cost model: ETH slippage 0.04%, SOL slippage 0.08%.
- Min hold 24h (3 settlements). Max hold 14 days (force re-evaluation).
- Max concurrent arbs: 2 (DP recommendation).
- Max position size: 20% of equity (DP recommendation).

**Statistical baseline (DeepSeek Task #9, 12-month BTC/ETH/SOL):**
- Frequency of rate > 0.05%/8h: BTC 4.2%, ETH 5.8%, SOL 12.7%
- Frequency of negative funding: BTC 28%, ETH 23%, SOL 19%
- BTC excluded from QA universe per v2.3.2 unified price gate

**Caveat:** DP did not provide raw query/code for the baseline analysis —
this is reconstruction. Real baseline will come from FundingMonitor running
14+ days against live Bybit data, then re-calibration.

**Status:** Active in commit #003. Will be re-calibrated after 14d live data.

---

## 2026-04-27 — funding_arb bug: double-counting in cost formula

**Bug:** Original cost formula was `leg_size × 2 × TOTAL_ROUND_TRIP_COST_PCT`
where TOTAL_ROUND_TRIP_COST_PCT already accounted for 4 transactions. This
double-counted the cost, causing all arb opportunities to be rejected.

**Fix in commit #003:** New `calc_round_trip_cost(symbol)` function that
correctly applies fees per leg without double-counting. Now:
- ETH round-trip cost: 0.43% (0.10% spot + 0.10% spot + 0.055% perp + 0.055% perp + 0.04%×4 slip + 0.03% tax)
- SOL round-trip cost: 0.59% (same fees, but 0.08%×4 slippage)

**Status:** Fixed.

---

## 2026-04-27 — funding_monitor opportunity emission bug

**Bug:** `_maybe_emit_opportunity` had dead code where `severity, direction
= "EXTREME", "LONG_GETS_PAID"` was assigned then immediately overwritten on
the next line. Also incorrect direction labelling for EXTREME positive funding.

**Fix in commit #003:**
- Removed dead code
- Fixed direction logic: positive funding = SHORT_GETS_PAID (longs pay shorts)
- Added clear docstring explaining funding mechanics

**Status:** Fixed.

---

## 2026-04-27 — Telegram trading commands: separate handler module

**Decision:** New `bot/handlers/trading_commands.py` separate from existing
`commands.py` (which handles macro pipeline). Avoids merge conflicts and
keeps prop trading concerns isolated.

**Commands added in commit #002:**
- `/balance`, `/funding`, `/earn`, `/earn_add`, `/earn_plan`, `/halt`,
  `/resume`, `/risk_status`, `/paper_pnl`, `/positions`

**Dependencies:** Injected at startup via `setup_trading_commands()`.

**Status:** Active.

---

## 2026-04-27 — Main entry point: bot/main.py + scheduler.py

**Decision:** Single `bot/main.py` is THE entry point.
- Loads .env config
- Constructs all components (Ledger, RiskKernel, EarnManager, FundingMonitor, FundingArbStrategy)
- Wires Telegram dispatcher
- Starts APScheduler with 5 jobs:
  1. funding_arb_evaluate every 15 min
  2. equity_snapshot every 1 hour
  3. daily_summary at 00:35 UTC
  4. weekly_summary Sunday 23:00 UTC
  5. earn_apr_check every 6 hours
- Handles graceful SIGTERM/SIGINT shutdown

**Why not extend old `bot.py` from /opt/qa_bot/:** That file is part of the
macro pipeline which was the old project focus. Cleaner to have prop trading
as its own self-contained module. Old `bot.py` can be retained for macro
QA reports if desired, run from a separate systemd service.

**Status:** Active in commit #003.

---

## 2026-04-27 — Capital allocation PDF: corrected per Task #10 verification

**Original PDF estimate:** Tier-2/3 USDT Flexible blended ~1% APR.

**Corrected per DeepSeek Task #10:**
- Tier-1 ($0-500): 12% (PROMO, not guaranteed; 7-12% typical)
- Tier-2 ($500-1000): 0.70%
- Tier-3 (>$1000): 0.28% — **lower than originally assumed 1.0%**

**Impact on $5K Flexible USDT allocation:**
- Original estimate: $90-105/year
- Corrected estimate: ~$74/year
  - $500 @ 12% = $60
  - $500 @ 0.70% = $3.50
  - $4,000 @ 0.28% = $11.20

**Decision:** PDF will be regenerated with corrected estimates. Allocation
strategy unchanged because liquidity buffer is the primary purpose of
Tier-2/3 holdings, not yield.

**Status:** Pending PDF regeneration in commit #003.

---

## Future decisions to log

- [ ] Strategy parameter recalibration methodology (after 14 days live data)
- [ ] Bayesian regime tracker integration (after 60 days)
- [ ] Symphony Orchestra refactor (after 5+ active agents)
- [ ] Public equity tracker format (after Phase 1 stable)
- [ ] DeepSeek Tasks #5/6/7 verification status (in progress)
- [ ] Live trading enable: criteria + procedure (documented in DEPLOY.md)
- [ ] basis_trade.py implementation (after funding_arb baseline)
- [ ] mean_reversion.py implementation (after funding_arb stable)
