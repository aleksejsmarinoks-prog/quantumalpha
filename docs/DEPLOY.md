# QuantumAlpha — Production Deployment Guide

Target: **Hetzner CX22 VPS (Ubuntu 24.04, 2 vCPU, 4GB RAM, 40GB SSD)**
Path: `/opt/qa_bot`
Service: `systemd` user service

This guide deploys `bot.main` from a fresh clone in **paper-mode**. Live trading and live Earn auto-mode are **opt-in** via env flags after manual verification.

---

## Prerequisites

- Hetzner Cloud account, CX22 VPS provisioned
- Domain (optional — only needed for HTTPS webhook, not used here since we use long-polling)
- SSH key configured

---

## 1. Initial server setup (one-time)

```bash
ssh root@YOUR_VPS_IP

# Update + essential packages
apt update && apt upgrade -y
apt install -y python3.11 python3.11-venv python3-pip git ufw fail2ban

# Lock down firewall — only SSH + maybe HTTPS for monitoring
ufw allow 22/tcp
ufw --force enable

# Create unprivileged user for the bot
adduser --disabled-password --gecos "" qabot
usermod -aG sudo qabot

# Switch to bot user
su - qabot
```

## 2. Clone repo + setup venv

```bash
cd ~
git clone https://github.com/aleksejsmarinoks-prog/quantumalpha.git
cd quantumalpha

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If `requirements.txt` is missing aiohttp/apscheduler/aiogram, install them:

```bash
pip install aiogram>=3.15 apscheduler>=3.10 aiohttp python-dotenv
```

## 3. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in at minimum:

```
TELEGRAM_TOKEN=YOUR_BOT_TOKEN_FROM_BOTFATHER
ALLOWED_USER_ID=YOUR_TELEGRAM_USER_ID
DATA_DIR=/home/qabot/quantumalpha/data
STARTING_EQUITY_USD=1000
LIVE_TRADING=false
LIVE_EARN_MODE=false
BYBIT_TESTNET=true
```

For initial deployment leave Bybit API keys EMPTY. The bot will run in paper-mode + public-endpoints-only mode and that's fully functional for the funding monitor baseline collection phase.

```bash
mkdir -p data
chmod 700 .env data
```

## 4. Test run (interactive)

Before installing as a service, verify the bot starts correctly:

```bash
source .venv/bin/activate
python -m bot.main
```

You should see:

```
================================================
QuantumAlpha bot starting
  data_dir       = /home/qabot/quantumalpha/data
  starting_eq    = $1,000.00
  live_trading   = False
  live_earn_mode = False
  bybit_testnet  = True
================================================
INFO     PnL Ledger initialised at .../data/pnl.db
INFO     RiskKernel initialised: equity=$1,000.00 ...
INFO     FundingMonitor started: symbols=['BTCUSDT', 'ETHUSDT', 'SOLUSDT'] interval=300s
INFO     Telegram polling started — bot is live
```

Test in Telegram by sending `/balance` — you should get a structured response with $1,000 equity and zero PnL.

Stop with `Ctrl+C`. The bot will gracefully shut down.

## 5. Install as systemd service

Create `/etc/systemd/system/quantumalpha.service` (use `sudo`):

```ini
[Unit]
Description=QuantumAlpha trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qabot
Group=qabot
WorkingDirectory=/home/qabot/quantumalpha
Environment="PATH=/home/qabot/quantumalpha/.venv/bin"
EnvironmentFile=/home/qabot/quantumalpha/.env
ExecStart=/home/qabot/quantumalpha/.venv/bin/python -m bot.main
Restart=on-failure
RestartSec=15s
StandardOutput=append:/home/qabot/quantumalpha/logs/quantumalpha.log
StandardError=append:/home/qabot/quantumalpha/logs/quantumalpha.err

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/qabot/quantumalpha/data /home/qabot/quantumalpha/logs

[Install]
WantedBy=multi-user.target
```

Then:

```bash
mkdir -p /home/qabot/quantumalpha/logs

sudo systemctl daemon-reload
sudo systemctl enable quantumalpha
sudo systemctl start quantumalpha

# Check status
sudo systemctl status quantumalpha
journalctl -u quantumalpha -f
```

## 6. Operations

### Restart after code update

```bash
cd ~/quantumalpha
git pull
sudo systemctl restart quantumalpha
```

### View logs

```bash
# Live tail
journalctl -u quantumalpha -f

# Last 100 lines
journalctl -u quantumalpha -n 100

# Errors only
journalctl -u quantumalpha -p err

# Filter by date
journalctl -u quantumalpha --since "1 hour ago"
```

### SQLite inspection

```bash
sqlite3 ~/quantumalpha/data/pnl.db
.tables
.schema trade_fills
SELECT COUNT(*) FROM trade_fills;
.quit

sqlite3 ~/quantumalpha/data/funding_history.db
SELECT symbol, AVG(funding_rate)*100 AS mean_pct, COUNT(*) FROM funding_rate_history GROUP BY symbol;
```

### Manual halt via Telegram

```
/halt 24 manual review
```

### Backup data dir

```bash
tar -czf backup-$(date +%Y%m%d).tar.gz ~/quantumalpha/data ~/quantumalpha/.env
```

Set up weekly backup via cron:

```cron
0 4 * * 0 cd /home/qabot/quantumalpha && tar -czf /home/qabot/backups/qa-$(date +\%Y\%m\%d).tar.gz data .env
```

---

## 7. Going live — transition criteria

The bot ships in **paper-mode**. Only enable `LIVE_TRADING=true` after ALL of these pass:

| Criterion | Threshold | How to check |
|---|---|---|
| Funding history collected | ≥ 14 days | `SELECT COUNT(*) FROM funding_rate_history;` should be ≥ 4000 (3 symbols × 12 samples/h × 24h × 14d ≈ 12,000) |
| DeepSeek Task #9 spec implemented | Done | thresholds calibrated against real distribution |
| Paper trades executed | ≥ 30 | `SELECT COUNT(*) FROM trade_fills WHERE is_paper = 1;` |
| Paper Sharpe ratio | ≥ 1.5 over 30 days | manual analysis |
| Max paper drawdown | ≤ 8% | manual analysis |
| Bybit API keys tested on testnet | All endpoints work | manual verification |

Once these pass:

1. Stop service: `sudo systemctl stop quantumalpha`
2. Edit `.env`: set `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET=false`, `LIVE_TRADING=true`
3. Restart: `sudo systemctl start quantumalpha`
4. Watch first 10 trades carefully via Telegram + journalctl

Same procedure for `LIVE_EARN_MODE=true` after testing on a small amount ($10) first.

---

## 8. Monitoring

The bot self-reports via Telegram. Recommended alerts:

- `/risk_status` daily — verify halt status, equity, DD
- `/balance` weekly — Earn yield + active trading PnL summary
- `/funding` ad-hoc — current rates + 7d stats

Set `journalctl` errors to email yourself if running unattended:

```bash
# Add to ~/.config/systemd/user/
# Or use external monitoring like Grafana + Promtail + Loki for production
```

---

## Troubleshooting

**Bot can't connect to Telegram**
→ Check TELEGRAM_TOKEN spelling. Test manually: `curl https://api.telegram.org/bot$TELEGRAM_TOKEN/getMe`

**Funding monitor returns no data**
→ Bybit may have rate-limited. Check `journalctl -u quantumalpha -p warning`. Public endpoints are 50 req/sec, should never throttle at 5-min poll.

**`/balance` shows $0**
→ STARTING_EQUITY_USD not set. Restart after editing .env.

**SQLite locked errors**
→ WAL mode is enabled, but ensure no other process accesses the same DB files. Don't run two bots against same DATA_DIR.

**Out of memory on CX22**
→ 4GB is enough for current scope. If it grows: upgrade to CX32 ($8/mo more) or add a swap file.
