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

**Alternatives rejected:**
- Continue building research/SaaS commercial platform: blocked by track record gap.
- Skip Earn layer to maximize trading capital: ignores inflation tax on idle USDT.
- Deploy full capital to active trading: violates max 25% per-position rule, no validation buffer.

**Status:** Active.

---

## 2026-04-27 — Strategy implementation order

**Context:** Need to choose first strategies for $1,000 capital.
Many options: funding arb, basis, mean reversion, momentum, scalping, grid.

**Decision:**
1. **P1 (week 1-3):** Funding rate arbitrage (delta-neutral)
2. **P1 (week 4-7):** Basis trade (calendar spread)
3. **P2 (week 5-8):** Mean reversion on panic dumps
4. **P3 (after 6 months live):** Scalping/momentum (only if P1+P2 stable)
5. **NEVER:** Martingale, grid without stop-out, DCA without max position cap

**Rationale:**
- Funding arb is mathematically defended (not directional).
- Basis is also delta-neutral.
- Mean reversion on calibrated panic dumps has 70-75% historical hit rate.
- Scalping requires $10K+ capital to overcome fee/spread drag.

**Status:** Active.

---

## 2026-04-27 — Risk Kernel design: hard caps over optimization

**Context:** Where to enforce risk limits — strategy level, kernel level, or both?

**Decision:** Centralized Risk Kernel with VETO power.
- All trade requests pass through `RiskKernel.approve_trade()`.
- No strategy can bypass kernel checks.
- Kernel limits are **hard caps** — no optimization (genetic algorithms, ESO, etc.)
  can modify them. This implements DeepSeek "Pattern 4: Hard Safety Constraints".
- Strategy-level filters are additional, kernel is not optional.

**Hard caps:**
- Max position 25% of equity
- Max total leverage 3x
- Daily DD 5% → 24h halt
- Weekly DD 10% → 7d halt
- Total DD 15% → indefinite halt + manual review
- 3 consecutive losses → 24h cooldown

**Status:** Active. Implemented in `bot/core/risk_kernel.py`.

---

## 2026-04-27 — PnL Ledger: SQLite, append-only, tax-grade

**Context:** Need single source of truth for trades, fees, funding, Earn positions.

**Decision:** SQLite with WAL mode, append-only event log.
- 7 tables: trade_fills, positions, funding_payments, earn_positions,
  earn_interest, equity_snapshots, metadata.
- Idempotent on `fill_id` to prevent duplicate inserts on retry.
- Tax-grade: every realization event tracked with cost basis, holding period.
- Reconcilable: full state can be replayed from event log.

**Alternatives rejected:**
- PostgreSQL: overkill for single-user system, more ops burden.
- CSV files: no idempotency, no concurrent reads, no transactions.
- In-memory dict: lost on crash, no audit trail.

**Status:** Active. Implemented in `bot/core/pnl_ledger.py`.

---

## 2026-04-27 — Bybit Earn API: read-only until verified

**Context:** DeepSeek research provided Earn API endpoint stubs but explicitly
flagged them as "interface partially hidden" / unverified.

**Decision:**
- Earn product subscription/redemption: **manual via Bybit UI** until endpoints verified.
- Earn position monitoring: track manually in PnL Ledger, reconcile against UI.
- Earn product discovery: monitor via web/UI, not API.
- Verify endpoints against current Bybit dev docs in Q3 2026 before automating.

**Alternatives rejected:**
- Implement based on DeepSeek's unverified endpoints: risk of failed orders or
  incorrect state on production capital.

**Status:** Active.

---

## Future decisions to log

- [ ] Strategy parameter calibration methodology (after 30 days live data)
- [ ] Bayesian regime tracker integration (after 60 days)
- [ ] Symphony Orchestra refactor (after 5+ active agents)
- [ ] Public equity tracker format (after Phase 1 stable)
- [ ] DeepSeek Tasks #5/6/7 verification status
