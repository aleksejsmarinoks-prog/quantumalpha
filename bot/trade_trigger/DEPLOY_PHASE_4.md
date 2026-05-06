# Phase 4 Deployment Guide

**What's new vs Phase 3.5:**
- ✅ **Anti-Bias gate LIVE** (auto-wires BybitClient if available, fail-safe to None)
- ✅ **4 government RSS sources**: WhiteHouse, OFAC, State Dept, Fed
- ✅ **`/tt_calibrate`** command — performance analysis & threshold tuning
- ✅ **Backtest harness** — `python -m bot.trade_trigger.backtest`
- ✅ **Logrotate config + automatic SQLite backups**

**Tests:** 153/153 in 3.7s. Backtest: 5/5 cases (100% classifier + fire accuracy).

---

## Step 1 — Backup current state

```bash
ssh qa@<vps>
cd /home/qa/quantumalpha
git status
git log -1

# Backup current trade_trigger
cp -r bot/trade_trigger bot/trade_trigger.phase35.bak
```

## Step 2 — Apply Phase 4

```bash
# Extract archive
tar xzf /tmp/qa_trade_trigger_phase4.tar.gz -C /tmp/

# Replace module
rm -rf bot/trade_trigger
mv /tmp/trade_trigger bot/trade_trigger

# Verify new files
ls bot/trade_trigger/calibration.py bot/trade_trigger/backtest.py
ls bot/trade_trigger/core_adapters/
ls bot/trade_trigger/sources/government.py bot/trade_trigger/sources/rss_base.py
ls bot/trade_trigger/ops/
```

## Step 3 — Install new dependency

```bash
source venv/bin/activate
pip install feedparser  # for RSS sources
```

## Step 4 — Run tests

```bash
python -m pytest bot/trade_trigger/tests -v
# Expected: 153 passed
```

## Step 5 — Run backtest

```bash
python -m bot.trade_trigger.backtest
# Expected:
# Classifier accuracy:   5/5 (100%)
# Fire-decision accuracy: 5/5 (100%)
```

If backtest passes, the classifier+pipeline is regression-clean.

## Step 6 — Optional new env vars

Add to `.env` if you want to control Phase 4 features:

```bash
# Anti-Bias gate auto-attempts BybitClient. Set 'off' to force-disable.
TT_ANTI_BIAS=on

# RSS sources auto-enable. Set 'off' to disable government feeds.
TT_RSS_ENABLED=on

# RSS poll interval (default 300s = 5min)
TT_RSS_INTERVAL_SEC=300
```

## Step 7 — Foreground smoke test

```bash
python -m bot.trade_trigger.bot_runner
```

**Expected log:**

```
[INFO] BybitClient initialized via pattern X         # OR warning that anti-bias disabled
[INFO] BybitProvider verified: ETHUSDT 2-kline...    # if anti-bias active
[INFO] Pipeline configured: L2=True, min_score=7.0, anti_bias=active|disabled
[INFO] Polymarket watcher configured: 8 markets...
[INFO] Government RSS watchers: 4 feeds (WhiteHouse, OFAC, StateDept, Fed)
[INFO] Scheduler started: polymarket every 180s, rss every 300s, heartbeat 60s
```

## Step 8 — Verify in Telegram

```
/start            → see Anti-Bias status (active or disabled)
/tt_status        → confirm sources count
/tt_help          → new /tt_calibrate listed
```

After 5-10 minutes:

```
/tt_sources       → should show 5 sources: polymarket + whitehouse + ofac + state_dept + fed
/tt_calibrate 1   → run calibration on 1-day window
```

## Step 9 — Install logrotate

```bash
sudo cp bot/trade_trigger/ops/logrotate.conf /etc/logrotate.d/quantumalpha
sudo logrotate -d /etc/logrotate.d/quantumalpha    # debug/dry-run
# If clean output, ready to use (logrotate runs daily via cron.daily)
```

## Step 10 — Install backup timer

```bash
chmod +x bot/trade_trigger/ops/backup.sh

# Test once manually
bot/trade_trigger/ops/backup.sh
ls /home/qa/backups/

# Install systemd timer
sudo cp bot/trade_trigger/ops/qa-backup.service /etc/systemd/system/
sudo cp bot/trade_trigger/ops/qa-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qa-backup.timer
systemctl list-timers qa-backup.timer
# Should show next run at 03:00 UTC
```

## Step 11 — Commit

```bash
cd /home/qa/quantumalpha
git add bot/trade_trigger/
git commit -m "feat(trade-trigger): Phase 4 — Anti-Bias + RSS + Calibration + Backtest

- Anti-Bias gate live wiring (BybitClient adapter, graceful fallback)
- 4 government RSS sources: WhiteHouse, OFAC, StateDept, Fed
- /tt_calibrate command for performance analysis
- Backtest harness with 5 historical cases (100% accuracy)
- Logrotate config + daily SQLite backup systemd timer

153/153 tests pass in 3.7s. Backtest validates Hormuz May 3 regression."

git push origin main
```

---

## Anti-Bias troubleshooting

If `/start` says "Anti-Bias disabled (BybitClient unavailable)":

1. **Check log:** look for "BybitClient init pattern X failed" or "no compatible klines method"
2. **Common cause:** BybitClient signature on your VPS differs from what the adapter probes.
   The adapter tries 4 init patterns and 4 method names. If none match, it falls back.
3. **Fix:** open `bot/core/bybit_client.py`, find your `get_klines`/`fetch_klines` method
   signature, and verify it matches one of the variants in
   `core_adapters/bybit_provider.py` (lines 60-100).
4. **Worst case:** disable explicitly with `TT_ANTI_BIAS=off`. System still functional with 4 gates.

## RSS troubleshooting

If `/tt_sources` shows RSS feeds with red status:

1. **OFAC feed URL** may differ slightly across years. Verify at https://ofac.treasury.gov/
2. **State Dept feed** sometimes returns HTML instead of XML on errors. feedparser handles
   gracefully but may produce empty entries.
3. **Fed feed** is reliable; if failing, check egress firewall on VPS.

For each source, see exact error in `last_error` column:
```bash
sqlite3 /home/qa/quantumalpha/data/trade_trigger.db \
  "SELECT source_name, consecutive_fails, last_error FROM source_health"
```
