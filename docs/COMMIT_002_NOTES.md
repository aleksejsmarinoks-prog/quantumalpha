# QuantumAlpha — Commit #002 Deploy Notes

## What's new in this commit

| File | Lines | Purpose |
|---|---|---|
| `bot/core/funding_monitor.py` | 465 | Background scanner for funding rates, baseline data collection |
| `bot/core/earn_manager.py` | 355 | Read-only Earn position tracker, blended APR calculator |
| `bot/strategies/__init__.py` | — | Package init |
| `bot/strategies/funding_arb.py` | 536 | Delta-neutral funding arbitrage strategy (paper-mode) |
| `bot/handlers/trading_commands.py` | 449 | New Telegram commands for prop trading |
| `bot/handlers/__init__.py` | — | Package init |
| `docs/DECISION_LOG.md` | — | Updated with commit #002 decisions |

**Total:** 1,805 new lines + tests passing.

---

## How to apply this commit

Same pattern as commit #001 — extract these files **next to** existing files
(not replacing — only `docs/DECISION_LOG.md` overwrites):

```bash
# 1. Download quantumalpha_commit_002.zip from chat
# 2. In your local repo:
cd ~/Documents/GitHub/quantumalpha
unzip ~/Downloads/quantumalpha_commit_002.zip -d /tmp/qa_commit_002
cp -R /tmp/qa_commit_002/quantumalpha_002/* .

# 3. Verify
ls bot/core/      # should now include: funding_monitor.py, earn_manager.py
ls bot/strategies # should include: funding_arb.py
ls bot/handlers   # should include: trading_commands.py
```

Or via GitHub Desktop: drag files from extracted folder into your local repo,
review changes, commit, push.

---

## Smoke tests (run on Mac before VPS deploy)

```bash
cd ~/Documents/GitHub/quantumalpha
pip install aiohttp
python -m bot.core.earn_manager      # tests Earn manager + gap analysis
python -m bot.strategies.funding_arb # tests funding arb decision logic
# (funding_monitor needs network access — test on VPS)
```

Expected:
- `earn_manager` shows summary + gap analysis vs $25K target
- `funding_arb` shows OPEN/SKIP decisions for sample funding rates

---

## Integration into bot.py (next commit)

This commit ships modules but **does not yet wire them into `bot.py`**.
That happens in **commit #003** which will:

1. Update `bot.py` to instantiate RiskKernel, PnLLedger, EarnManager,
   FundingMonitor, FundingArbStrategy at startup
2. Pass dependencies into `trading_commands.setup_trading_commands(...)`
3. Add `dp.include_router(trading_commands.router)`
4. Update `core/scheduler.py` to start FundingMonitor and run FundingArbStrategy
   evaluation cycle every N minutes
5. Add `.env` entries for new strategies

Until commit #003 is applied:
- New modules are importable but not active
- Existing macro pipeline keeps working
- No breaking changes to current bot

---

## What you need to do AFTER applying commit #002

1. **Create `data/` directory** on VPS (will hold SQLite DBs):
   ```bash
   mkdir -p /opt/qa_bot/data
   ```

2. **Don't restart bot yet** — wait for commit #003 which wires everything together.

3. **Continue baseline data collection** prep:
   - Bybit Data Export → 3 CSV files for DeepSeek Task #6
   - Awaits DeepSeek Task #8 (Earn API verification)
   - Awaits DeepSeek Task #9 (Funding arb spec)

---

## Key behavior to know

### FundingMonitor
- Polls every 5 min by default (configurable)
- Stores to `data/funding_history.db` (separate from PnL ledger)
- Emits opportunity events when rate crosses thresholds
- 7-14 days of data → real baseline distribution for calibration

### EarnManager
- **Read-only** — does NOT subscribe to Earn products
- User subscribes manually on Bybit UI, then `/earn_add` to record
- Calculates blended APR, daily/monthly earnings estimates
- Alerts on positions expiring within 7 days

### FundingArbStrategy
- **Paper mode by default** (`LIVE_TRADING=false`)
- Decision logic complete and tested
- Cost model: 0.42% round-trip (4 transactions × taker fee + slippage)
- Economic gate: 3-day expected funding > 1.5x round-trip cost
- BTC excluded; only ETHUSDT and SOLUSDT in v1.0 universe

### Trading commands
- All require `ALLOWED_USER_ID` match
- Independent of macro pipeline — can be added/removed safely
- Dependency-injected for testability
