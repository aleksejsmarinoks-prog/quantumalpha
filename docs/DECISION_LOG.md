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

## 2026-04-27 — Bybit Earn API: read-only until verified

**Decision:** No automatic Earn subscription. User subscribes manually via UI,
records to bot via `/earn_add` command. EarnManager tracks positions,
computes blended APR, alerts on expiring lockups.

**Status:** Active. Will revisit after DeepSeek Task #8 verification.

---

## 2026-04-27 — Funding Monitor: separate DB for market data

**Context:** Funding rate observations are market data, distinct from our
PnL ledger which tracks our transactions.

**Decision:** Funding rates stored in separate `data/funding_history.db`,
NOT in pnl.db. Includes:
- `funding_rate_history` table — every snapshot, dedupes per settlement
- `funding_opportunities` table — events when threshold crossed

**Rationale:** Cleaner schema separation. PnL ledger remains tax-focused.
Market data can be analyzed/exported independently.

**Status:** Active. Implemented in `bot/core/funding_monitor.py`.

---

## 2026-04-27 — Funding Arb: paper-mode default, live requires explicit enable

**Decision:**
- `LIVE_TRADING=false` by default in `.env.example`
- All fills recorded to ledger with `is_paper=True` flag
- Real order routing NOT implemented in v1.0 (deferred until baseline data
  + DeepSeek Task #9 calibration)

**Initial calibration (placeholders, expect Task #9 to refine):**
- Open threshold: 0.05%/8h (~54% APR equivalent)
- Close threshold: 0.01%/8h
- Position size: $200 per leg ($400 total exposure)
- Min hold: 8 hours (1 settlement)
- Cost model: 4 transactions × (0.055% taker + 0.05% slippage) = 0.42%
  total round-trip
- Economic gate: expected 3-day funding > 1.5x round-trip cost

**BTC permanently excluded** per QA v2.3.2 unified price gate.

**Status:** Active. Implemented in `bot/strategies/funding_arb.py`.

---

## 2026-04-27 — Telegram trading commands: separate handler module

**Decision:** New `bot/handlers/trading_commands.py` separate from existing
`commands.py` (which handles macro pipeline). Avoids merge conflicts and
keeps prop trading concerns isolated.

**Commands added:**
- `/balance` — capital allocation across active/passive/reserves
- `/funding` — current rates + opportunities + 7d stats
- `/earn` — Earn layer summary
- `/earn_add COIN AMOUNT TYPE APR [DAYS]` — record manual subscription
- `/earn_plan` — gap analysis vs $25K target
- `/halt [HOURS] [REASON]` — manual trading halt
- `/resume` — resume after manual halt (only valid for manual halts)
- `/risk_status` — kernel state, kill switches, DD
- `/paper_pnl` — paper trading summary
- `/positions` — open arb positions

**Dependencies:** Injected at startup via `setup_trading_commands()` to keep
commands testable and uncoupled.

**Status:** Active. Implemented in `bot/handlers/trading_commands.py`.

---

## Future decisions to log

- [ ] Strategy parameter calibration methodology (after 30 days live data)
- [ ] Bayesian regime tracker integration (after 60 days)
- [ ] Symphony Orchestra refactor (after 5+ active agents)
- [ ] Public equity tracker format (after Phase 1 stable)
- [ ] DeepSeek Tasks #5/6/7/8/9 verification status
- [ ] Live trading enable: criteria + procedure
- [ ] basis_trade.py implementation (after funding_arb baseline)
- [ ] mean_reversion.py implementation (after funding_arb stable)
