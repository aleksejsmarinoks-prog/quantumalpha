# QuantumAlpha — Production Deployment Guide

This is the **deployed reality** as of 2026-05-06. Earlier revisions of this document referenced `/opt/qa_bot`, user `qabot`, service `quantumalpha.service`, and Hetzner CX22 — none of those match the running system. If you find more drift, fix it here.

---

## 1. Host

| | |
|---|---|
| Provider | Hetzner Cloud |
| Plan | **CPX22** (2 vCPU AMD, 4 GB RAM, 40 GB NVMe) |
| OS | Ubuntu 24.04 LTS |
| Public IPv4 | `167.235.254.33` |
| User | `qa` (unprivileged, member of `sudo`) |
| App home | `/home/qa/quantumalpha` |
| Python | `python3` → 3.12.3 (system) |
| Virtualenv | `/home/qa/quantumalpha/venv` |

---

## 2. SSH access

```bash
ssh qa@167.235.254.33
```

Key-based auth only; password login is disabled. The `qa` user holds a deploy key registered on the GitHub repo for `git pull`. Do not use a personal key for git operations from the VPS.

---

## 3. Systemd unit

Path: `/etc/systemd/system/qa-bot.service`. Current content (verbatim):

```ini
[Unit]
Description=QuantumAlpha Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qa
Group=qa
WorkingDirectory=/home/qa/quantumalpha
EnvironmentFile=/home/qa/quantumalpha/.env
ExecStart=/home/qa/quantumalpha/venv/bin/python -m bot.main
Restart=always
RestartSec=10
StandardOutput=append:/home/qa/quantumalpha/logs/qa.log
StandardError=append:/home/qa/quantumalpha/logs/qa.log

[Install]
WantedBy=multi-user.target
```

Key points: stdout and stderr both append to a single `qa.log` (no journald-only output, log file is the source of truth); `Restart=always` with 10s back-off; no security hardening directives are currently applied (room to add `NoNewPrivileges`, `ProtectSystem`, etc. — tracked separately).

---

## 4. Environment variables

Stored in `/home/qa/quantumalpha/.env` (mode `600`, owner `qa`). The systemd unit loads it via `EnvironmentFile=`. Names only — populate values from your password manager:

```
# Telegram
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
ALLOWED_USER_ID

# Bybit
BYBIT_API_KEY
BYBIT_API_SECRET
BYBIT_TESTNET

# Mode flags
QA_MODE
LIVE_TRADING
QA_LIVE_EARN_MODE

# Capital + risk
QA_TOTAL_CAPITAL_USD
RISK_PER_TRADE_PCT
DAILY_DD_LIMIT_PCT
WEEKLY_DD_LIMIT_PCT

# Strategy toggles
STRATEGY_FUNDING_ARB_ENABLED
STRATEGY_BASIS_TRADE_ENABLED
STRATEGY_MEAN_REVERSION_ENABLED

# Macro alerts
VIX_ALERT_THRESHOLD
GOLD_MOVE_PCT_ALERT
OIL_MOVE_PCT_ALERT

# Reporting / scheduling
EQUITY_TRACKER_SHEET_ID
EQUITY_SNAPSHOT_INTERVAL_MIN
PIPELINE_CRON

# Misc
ANTHROPIC_API_KEY
```

Never commit `.env`. `.env.example` in the repo is the template. Live trading and live-Earn must be opted into explicitly (`LIVE_TRADING=true`, `QA_LIVE_EARN_MODE=true`) — both default to false.

---

## 5. Deployment workflow

Routine code update:

```bash
ssh qa@167.235.254.33
cd ~/quantumalpha
git pull
# only if requirements.txt changed:
source venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart qa-bot.service
```

Verify the restart took:

```bash
sudo systemctl status qa-bot.service --no-pager
tail -n 50 ~/quantumalpha/logs/qa.log
```

Look for the `QuantumAlpha bot starting` banner and `Telegram polling started — bot is live` within the first ~2 seconds.

---

## 6. Common ops

```bash
# Service control
sudo systemctl status qa-bot.service
sudo systemctl restart qa-bot.service
sudo systemctl stop qa-bot.service
sudo systemctl start qa-bot.service

# Live tail (file is authoritative)
tail -f ~/quantumalpha/logs/qa.log

# journald view (also captures pre-ExecStart failures)
journalctl -u qa-bot.service -n 200 --no-pager
journalctl -u qa-bot.service -f

# Manual debug run (stop the service first to avoid double-polling Telegram)
sudo systemctl stop qa-bot.service
cd ~/quantumalpha && source venv/bin/activate && python -m bot.main

# SQLite inspection (install once: sudo apt install -y sqlite3)
sqlite3 ~/quantumalpha/data/pnl.db '.tables'
sqlite3 ~/quantumalpha/data/pnl.db 'SELECT COUNT(*) FROM trade_fills;'
sqlite3 ~/quantumalpha/data/funding.db 'SELECT symbol, COUNT(*) FROM funding_rate_history GROUP BY symbol;'
```

---

## 7. Logs

Single combined file: `/home/qa/quantumalpha/logs/qa.log`. Both stdout and stderr from the systemd unit append here, plus everything Python's `logging` module emits.

- No rotation is currently configured. The file is ~6 MB after one week — plan to add `logrotate` before it crosses ~100 MB.
- For pre-`ExecStart` failures (env-file parse errors, venv missing) consult `journalctl -u qa-bot.service` instead — the file won't have them.

---

## 8. Going live — transition criteria

The bot ships in paper-mode. Flip `LIVE_TRADING=true` only when **all** of these pass:

| Criterion | Threshold | How to check |
|---|---|---|
| Funding history collected | ≥ 14 days | `SELECT COUNT(*) FROM funding_rate_history;` ≥ ~12,000 (3 symbols × 12/h × 24h × 14d) |
| Paper trades executed | ≥ 30 | `SELECT COUNT(*) FROM trade_fills WHERE is_paper = 1;` |
| Paper Sharpe (30d rolling) | ≥ 1.5 | manual analysis from `equity_snapshots` |
| Max paper drawdown | ≤ 8% | manual analysis |
| Bybit testnet end-to-end | All endpoints OK | manual verification with `BYBIT_TESTNET=true` |

Procedure:

```bash
sudo systemctl stop qa-bot.service
nano ~/quantumalpha/.env  # set BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET=false, LIVE_TRADING=true
sudo systemctl start qa-bot.service
tail -f ~/quantumalpha/logs/qa.log  # watch first ~10 trades closely
```

Same procedure for `QA_LIVE_EARN_MODE=true`, ideally first on a small ($10) Earn balance.

---

## 9. Automated backups (Phase A — local)

Daily snapshot of the SQLite databases, `.env`, and the tail of `qa.log`. Phase B (off-site rotation to Hetzner Storage Box) is planned but not yet wired up.

- **Script:** `scripts/backup.sh`
- **Destination:** `/home/qa/backups/qa-backup-YYYYMMDD-HHMM.tar.gz` (mode `600`, dir mode `700`)
- **Includes:** `data/funding.db`, `data/pnl.db`, `.env`, last 1000 lines of `logs/qa.log`
- **Retention:** keep newest 7, delete older
- **Audit log:** `/home/qa/backups/backup.log` (append-only)
- **Cron:** daily 03:00 UTC, installed in the `qa` user crontab:

```cron
0 3 * * * /home/qa/quantumalpha/scripts/backup.sh >> /home/qa/backups/backup.log 2>&1
```

Verify with `crontab -l`. Manual run: `/home/qa/quantumalpha/scripts/backup.sh && ls -lh /home/qa/backups/`.

### Phase B — off-site (planned, not implemented)

- Hetzner Storage Box subscription (~€4/mo)
- `rclone` over SFTP or WebDAV
- Rotation: 30 daily / 12 weekly / 12 monthly
- At-rest encryption with `age` or `gpg` before upload

---

## 10. Troubleshooting

**Bot can't connect to Telegram.** Check `TELEGRAM_BOT_TOKEN`. Sanity check: `curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe` (run from a host with the same key).

**`portfolio_sync failed: Auth required but BYBIT_API_KEY/BYBIT_API_SECRET not set`.** Expected when running keyless in paper-mode; the scheduler job should short-circuit instead of throwing — tracked as a known issue.

**`'BybitClient' object has no attribute 'get_klines'` every 5 min.** Method missing on the client; kline-dependent strategies (e.g. `cvd_divergence`) silently degrade. Add `get_klines` to `bot/core/bybit_client.py`.

**SQLite `database is locked`.** WAL mode is enabled — symptom usually means a second process is accessing the same DB. Don't run two bots against `~/quantumalpha/data` (e.g. don't leave a manual debug run going while the service is up).

**Out of memory.** 4 GB CPX22 is comfortable for current scope. If it tightens, upgrade plan or add a 2 GB swap file before rewriting code.
