# QuantumAlpha — Commit #003 Notes

## What's new

| File | Change | Purpose |
|---|---|---|
| `bot/main.py` | NEW | Main entry point — wires everything |
| `bot/scheduler.py` | NEW | APScheduler with 5 periodic jobs |
| `bot/core/bybit_client.py` | UPDATED | Added verified Earn API methods |
| `bot/core/earn_manager.py` | UPDATED | v1.1 — optional live mode (gated) |
| `bot/core/funding_monitor.py` | BUG FIX | dead code in `_maybe_emit_opportunity` |
| `bot/strategies/funding_arb.py` | RECALIBRATED | per DeepSeek Task #9 + safety overrides |
| `.env.example` | REWRITTEN | All commit #003 config keys |
| `docs/DEPLOY.md` | NEW | Production deploy guide for Hetzner CX22 |
| `docs/DECISION_LOG.md` | UPDATED | All commit #003 decisions logged |
| `docs/COMMIT_003_NOTES.md` | NEW | This file |

---

## Critical changes vs. commit #002

### 1. funding_arb thresholds RECALIBRATED

**Before (commit #002, placeholder):**
- Open: 0.05%/8h universal
- Close: 0.01%/8h universal
- Cost: 0.42% round-trip (taker only)

**After (commit #003, per DP Task #9):**
- Open: 0.040%/8h ETH, 0.050%/8h SOL (per-symbol)
- Close: 0.012%/8h ETH, 0.015%/8h SOL
- Cost: 0.43% ETH, 0.59% SOL (proper per-symbol slippage)
- Min hold: 24h (3 settlements). Max hold: 14 days.
- Max concurrent arbs: 2
- Max position: 20% of equity

**Why I overrode DP's 0.028%/8h recommendation:**
DP's number is exactly at 3-day break-even. Operating at break-even = zero
expected edge, all variance is noise. Need 1.5x safety margin → 0.040%/8h.

### 2. earn_manager v1.1 supports auto-mode

**Before:** Read-only. User subscribes manually, records via `/earn_add`.

**After:** Read-only by DEFAULT. If `LIVE_EARN_MODE=true` AND Bybit API keys
provided, can call `/v5/earn/place-order` for FlexibleSaving + OnChain.
Verified per DeepSeek Task #8 against Bybit V5 docs.

`fixed_term`, `dual_asset`, `discount_buy`, `launchpool` STILL require
manual UI subscription (no API endpoint).

### 3. bug fix: funding_monitor opportunity emission

Old code had:
```python
severity, direction = "EXTREME", "LONG_GETS_PAID"  # ← never reached
severity, direction = "EXTREME", "SHORT_GETS_PAID"  # ← always overwrites
```

Fixed direction labelling for funding mechanics:
- POSITIVE funding → SHORT receives (longs pay shorts)
- NEGATIVE funding → LONG receives

### 4. New entry point: `bot/main.py`

This is **the** entry point. Run: `python -m bot.main`

It:
- Loads .env
- Constructs everything (Ledger, RiskKernel, BybitClient, EarnManager,
  FundingMonitor, FundingArbStrategy)
- Sets up Telegram dispatcher with trading_commands router
- Starts APScheduler with 5 cron jobs
- Handles graceful shutdown on SIGTERM/SIGINT

---

## How to apply

### On your Mac

```bash
cd ~/Documents/GitHub/quantumalpha
# Extract commit 003 over the existing repo
cp -R ~/Downloads/quantumalpha_003/* .
# (or unzip if you got a zip)

# Verify
ls bot/                # should now have main.py, scheduler.py
ls docs/               # should now have DEPLOY.md, COMMIT_003_NOTES.md

# Smoke tests (no network needed)
python -m bot.core.earn_manager        # ✅ should print summary + gap analysis
python -m bot.strategies.funding_arb   # ✅ should pass decisions
python -c "from bot.main import QuantumAlphaBot; print('OK')"

# Push
git add .
git status
git commit -m "Commit #003: bot/main.py wiring, recalibrated funding_arb, earn live mode, deploy guide"
git push origin main
```

### On the VPS (after `git pull`)

See `docs/DEPLOY.md` for first-time setup. For updates:

```bash
cd ~/quantumalpha
git pull
sudo systemctl restart quantumalpha
journalctl -u quantumalpha -f
```

---

## Verification checklist

After applying, run these to confirm everything works:

### Local (Mac)

```bash
# Constructor smoke test (no real Telegram needed)
TELEGRAM_TOKEN=dummy ALLOWED_USER_ID=12345 DATA_DIR=/tmp/qa python -c "
import os
os.environ['TELEGRAM_TOKEN']='dummy'
os.environ['ALLOWED_USER_ID']='12345'
from bot.main import QuantumAlphaBot, load_config
app = QuantumAlphaBot(load_config())
print('Bot constructs cleanly')
print(f'  scheduler jobs: {len(app.scheduler.get_jobs())}')
"
```

Expected:
```
Bot constructs cleanly
  scheduler jobs: 5
```

### VPS (after first deploy)

After 1 hour:
```bash
sqlite3 ~/quantumalpha/data/funding_history.db \
  'SELECT symbol, COUNT(*) FROM funding_rate_history GROUP BY symbol;'
```

Should show ~12 samples per symbol (5-min poll × 60 min).

After 24 hours: ~288 per symbol.

After 14 days: ~4,000 per symbol — enough for re-calibration.

---

## What this commit does NOT yet include

- [ ] Daily/weekly Telegram summary content (job stubs only — implement in #004)
- [ ] basis_trade.py strategy
- [ ] mean_reversion.py strategy
- [ ] Bybit funding history bulk import (for backtesting before 14d live data)
- [ ] Earn live-mode Telegram commands `/earn_subscribe`, `/earn_redeem`
- [ ] Position reconciliation against Bybit API on startup
- [ ] Real `/earn_interest` recording from Bybit transaction-log

These are **commit #004** scope.
