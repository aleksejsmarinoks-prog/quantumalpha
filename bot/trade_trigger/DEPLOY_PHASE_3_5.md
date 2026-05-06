# Phase 3.5 Deployment Guide — Manual Foreground Run

**Goal:** verify bot_runner end-to-end before enabling systemd auto-start.

**Status when this guide is followed:**
- v0.3.5 deployed, 105/105 tests passed in 1.5s
- Polymarket source LIVE every 3 min
- 4-gate pipeline (velocity, classifier, mapping, corroboration)
- Anti-Bias gate disabled (Phase 4)

---

## Prerequisites checklist

Before starting, verify on VPS:

```bash
ssh qa@<vps>
cd /home/qa/quantumalpha

# 1. Phase 3 already deployed (commit 0ecf03e)
git log -1 --oneline
# Expected: 0ecf03e or later

# 2. Tests still pass
python -m pytest bot/trade_trigger/tests -q
# Expected: 83 passed (will become 105 after Phase 3.5)

# 3. DB initialized
ls -la data/trade_trigger.db
# Expected: ~108 KB

# 4. ANTHROPIC_API_KEY in .env (verified Day 3)
grep -c "^ANTHROPIC_API_KEY=" .env
# Expected: 1
```

If any of these fail, do NOT proceed. Resolve first.

---

## Step 1 — Apply Phase 3.5 archive

```bash
cd /home/qa/quantumalpha
git checkout -b feature/trade-trigger-phase-3-5
# (or stay on main if you prefer single branch)

# Backup existing trade_trigger before overwriting
cp -r bot/trade_trigger bot/trade_trigger.phase3.bak

# Extract phase 3.5 archive (replace path to where you uploaded it)
tar xzf /tmp/qa_trade_trigger_phase3_5.tar.gz -C /tmp/
# Verify expected structure
ls /tmp/trade_trigger/

# Replace the module
rm -rf bot/trade_trigger
mv /tmp/trade_trigger bot/trade_trigger

# Quick sanity check
ls bot/trade_trigger/bot_runner.py bot/trade_trigger/alerts.py
# Both should exist
```

## Step 2 — Run tests in production env

```bash
source venv/bin/activate
python -m pytest bot/trade_trigger/tests -v
# Expected: 105 passed in <2s
```

If tests fail, **stop here** and report failures. Do not proceed.

## Step 3 — Add bot env vars

```bash
nano .env
```

Append (replace TOKEN with real value from your Notes):

```
# ─── Phase 3.5: QA Trade Trigger ───
TELEGRAM_BOT_TOKEN_TT=<token_from_notes>
TELEGRAM_CHAT_ID_TT=1254741315
AI_LAYER_ENABLED=true
AI_ADVISOR_THRESHOLD=0.7
TT_DB_PATH=data/trade_trigger.db
TT_POLL_INTERVAL_SEC=180
# Optional override: TT_MIN_SCORE=7.0  (default = 7.0 with L2, 5.0 without)
# Optional logging:  TT_LOG_LEVEL=INFO
```

```bash
chmod 600 .env  # confirm permission
```

## Step 4 — Foreground smoke test

```bash
cd /home/qa/quantumalpha
source venv/bin/activate
python -m bot.trade_trigger.bot_runner
```

**Expected log output:**

```
2026-05-06 ... [INFO] trade_trigger.bot: .env loaded from /home/qa/quantumalpha/.env
2026-05-06 ... [INFO] trade_trigger.bot: Pipeline configured: L2=True, min_score=7.0, anti_bias=disabled
2026-05-06 ... [INFO] trade_trigger.bot: Polymarket watcher configured: 8 markets, 180s interval
2026-05-06 ... [INFO] trade_trigger.bot: Scheduler started: polymarket every 180s, heartbeat every 60s
2026-05-06 ... [INFO] trade_trigger.bot: QA Trade Trigger entering polling loop
```

**In Telegram you should receive:**
> QA Trade Trigger started (v0.3.5)
> Polling Polymarket every 180s.
> Use /tt_status to verify.

## Step 5 — Manual verification commands

In Telegram, send these one by one:

### `/start`
Welcome message describing 4 active gates.

### `/tt_status`
```
QA Trade Trigger — STATUS (v0.3.5)
────────────────────────────────
Events  total/24h:  0 / 0
Signals total/24h:  0 / 0
Actionable cls:     0
Sources healthy:    0 / 0
...
```

(Counts will be 0 until first poll completes — wait 3 minutes)

### After 3-5 minutes — `/tt_status` again
Sources should show 1 (polymarket) healthy.

### `/tt_sources`
```
SOURCE HEALTH
────────────────────────────────
polymarket  🟢 OK
  polls=1  events=0
  last_ok=05-06 08:30
```

### `/tt_recent`
Empty until first event fires (no signals yet — Polymarket needs ≥2 polls
to detect a shift, so first signal earliest at T+6min after a real shift).

### `/tt_help`
Full commands list.

## Step 6 — Verify polling working

In a second SSH terminal:

```bash
# Watch DB activity
watch -n 30 'sqlite3 /home/qa/quantumalpha/data/trade_trigger.db \
  "SELECT source_name, total_polls, total_events, last_poll_utc FROM source_health"'
```

After ~10 min you should see `total_polls=3-4` and counters updating.

## Step 7 — Stop and review

Hit `Ctrl+C` in foreground terminal. Should see:

```
^C
2026-05-06 ... [INFO] trade_trigger.bot: Shutdown signal received
2026-05-06 ... [INFO] trade_trigger.bot: Bot shutdown complete
```

Review log for any unexpected warnings/errors. Fix or report issues.

---

## Step 8 — Enable systemd (only after Steps 1-7 succeed)

```bash
sudo cp bot/trade_trigger/qa-trade-trigger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable qa-trade-trigger.service
sudo systemctl start qa-trade-trigger.service

# Verify
sudo systemctl status qa-trade-trigger.service
# Expected: active (running)

# Tail logs
sudo journalctl -u qa-trade-trigger.service -f
# OR
tail -f /home/qa/quantumalpha/logs/trade_trigger.log
```

In Telegram you should receive the startup message again.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `TELEGRAM_BOT_TOKEN_TT not set — aborting` | env not loaded | Verify .env, check permissions |
| Pipeline reports `L2=False` despite AI_LAYER_ENABLED=true | API key not visible | `cat .env \| grep ANTHROPIC` |
| No alerts after hours | No real shifts on Polymarket | Normal — markets quiet on flat days |
| `/tt_status` shows 0 sources | Scheduler not started | Check log for "Scheduler started" |
| Telegram says "Unauthorized" | Wrong CHAT_ID | Verify your numeric ID via @userinfobot |
| `polymarket_cycle error` in logs | Polymarket API timeout/format change | Check `https://gamma-api.polymarket.com/markets` is reachable |

---

## What to expect realistically

**First 24 hours:**
- 1-3 Polymarket events MIGHT trigger if there's macro news
- Most polls return zero events (markets sit flat between catalysts)
- This is normal — the system is designed to fire **rarely but accurately**

**First week:**
- ~5-15 alerts depending on news flow
- Track precision: how many of fires were good entries (use `/tt_recent` and confirm/skip buttons)
- After 30 days enough data to calibrate `TT_MIN_SCORE` and `shift_threshold` per-market

## Phase 4 preview (after 1+ week of Phase 3.5 stable)

- Wire BybitClient → Anti-Bias gate (enables RSI/intraday checks)
- Add MT Newswires MCP source (institutional speed)
- Add WhiteHouse + OFAC RSS feeds
- Add `/tt_calibrate` command for tuning thresholds based on your fire history
