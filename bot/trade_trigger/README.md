# QA Trade Trigger

**Module version:** 0.2.0 (Phase 3 — Polymarket + Pipeline orchestrator)
**Status:** Tests pass 83/83 in 1.3s. Polymarket source LIVE. Other sources Phase 3.5.
**Target deployment:** `/home/qa/quantumalpha/bot/trade_trigger/`

---

## What this module does

Catches **early geopolitical / macro events** before the FOMO crowd and
pushes trade-actionable alerts to Telegram Bot #2 (separate from main
trading bot). Rationale: by the time a "BTC +5% in 30 min" headline hits
mainstream feeds, the squeeze is over. We want to fire 5-30 minutes
**before** that, with concrete tickers + size + invalidation level.

This is **not a FOMO chaser**. It's an **Early Event Capture** system.
The Anti-Bias Gate explicitly skips signals if RSI > 70 or intraday
move > 5% in target direction.

---

## Architecture

```
                    NewsEvent
                        │
           ┌────────────┼────────────┐
           │            │            │
       [Velocity]  [Classifier]  [SQLite]
           │       L1 + L2          insert
           │            │            │
           └─────►  decision  ◄──────┘
                        │
            ┌───────────┼───────────┐
            │           │           │
       [Corrobor.] [Anti-Bias]  [Mapping]
        2× T1 src   live RSI    event→tickers
            │           │           │
            └───────────┼───────────┘
                        │
                  TradeSignal
                        │
                ┌───────┴───────┐
                │               │
            Telegram         SQLite
            Bot #2          audit log
```

Five filter gates (any single failure → no fire):

| Gate | What it checks | Module |
|---|---|---|
| Velocity | Event ≤ 2h old | `filters/velocity_tracker.py` |
| Classifier | L1 heuristic + L2 Claude → score ≥ 7 | `classifier.py` |
| Corroboration | ≥ 2 distinct Tier-1 sources OR direct authoritative source | `filters/corroboration_gate.py` |
| Anti-Bias | RSI ≤ 70, intraday move ≤ 5% in trigger direction | `filters/anti_bias_check.py` |
| Mapping | Event type → at least 1 non-excluded ticker | `trade_trigger_mapping.py` |

---

## File map

```
bot/trade_trigger/
├── __init__.py                    Public API
├── models.py                      NewsEvent, TradeSignal, etc.
├── trade_trigger_mapping.py       31 event types → tickers
├── classifier.py                  L1 heuristic + L2 Claude API
│                                  (incl. polymarket-specific keyword rules)
├── pipeline.py                    PipelineOrchestrator — main glue [Phase 3]
├── db.py                          SQLite layer (events / classifications /
│                                  signals / audit / polymarket_odds_history)
├── bot_runner.py                  Telegram Bot #2 (aiogram 3.27)
├── filters/
│   ├── velocity_tracker.py        Stale event rejector
│   ├── corroboration_gate.py      Cross-source verifier (closes QA spec gap)
│   └── anti_bias_check.py         Live RSI/price gate (closes QA spec gap)
├── sources/
│   └── polymarket.py              Polymarket odds shift detector [Phase 3]
└── tests/
    ├── conftest.py                Pytest fixtures
    ├── test_classifier.py         Including REAL Hormuz May 3 event
    ├── test_db.py
    ├── test_filters.py
    ├── test_mapping.py
    ├── test_polymarket.py         [Phase 3]
    └── test_pipeline.py           [Phase 3]
```

---

## Deployment to VPS

**Prerequisites confirmed verified on VPS (May 4, 2026):**
- Path: `/home/qa/quantumalpha/`
- User: `qa`
- Service: `qa-bot.service`
- ANTHROPIC_API_KEY: ✅ in .env
- Python 3.12 venv at `venv/`
- aiogram 3.27 ✅

### Step 1 — Get Telegram bot credentials (USER ACTION)

```
@BotFather
  /newbot
  Name: QA Trade Trigger
  Username: <unique>_qa_trigger_bot
→ TOKEN received
```

Get your numeric chat_id (same as for main bot, or `@userinfobot`).

### Step 2 — Update .env on VPS

```bash
ssh qa@<vps>
cd /home/qa/quantumalpha
nano .env

# Add:
TELEGRAM_BOT_TOKEN_TT=<token from BotFather>
TELEGRAM_CHAT_ID_TT=<your numeric chat_id>
AI_LAYER_ENABLED=true
AI_ADVISOR_THRESHOLD=0.7
TT_DB_PATH=data/trade_trigger.db

chmod 600 .env  # already set, double-check
```

### Step 3 — Create feature branch and copy files

```bash
cd /home/qa/quantumalpha
git checkout -b feature/trade-trigger
git pull --rebase  # safety

# Copy module files (assumes you've extracted the archive into bot/trade_trigger/)
ls bot/trade_trigger/  # verify all files present

# Copy pytest.ini to project root
mv bot/trade_trigger/pytest.ini ./pytest.ini  # or leave both, root takes priority
```

### Step 4 — Install dependencies

```bash
source venv/bin/activate
pip install -r bot/trade_trigger/requirements_additions.txt --break-system-packages

# Verify
python -c "import anthropic; print(anthropic.__version__)"
python -c "import pytest; print(pytest.__version__)"
python -c "import feedparser; print(feedparser.__version__)"
```

### Step 5 — Run tests

```bash
cd /home/qa/quantumalpha
python -m pytest bot/trade_trigger/tests -v

# Expected: 60 passed in <1s
```

### Step 6 — Initialize DB

```bash
python -c "from bot.trade_trigger.db import TradeTriggerDB; TradeTriggerDB('data/trade_trigger.db')"
ls -la data/trade_trigger.db  # should exist, ~24 KB
```

### Step 7 — Smoke-test bot_runner manually

```bash
python -m bot.trade_trigger.bot_runner
# In Telegram: /start, /tt_status, /tt_help
# Stop with Ctrl+C
```

### Step 8 — Create systemd service

```bash
sudo nano /etc/systemd/system/qa-trade-trigger.service
```

Paste:

```ini
[Unit]
Description=QuantumAlpha Trade Trigger Bot
After=network-online.target qa-bot.service
Wants=network-online.target

[Service]
Type=simple
User=qa
Group=qa
WorkingDirectory=/home/qa/quantumalpha
EnvironmentFile=/home/qa/quantumalpha/.env
ExecStart=/home/qa/quantumalpha/venv/bin/python -m bot.trade_trigger.bot_runner
Restart=always
RestartSec=10
StandardOutput=append:/home/qa/quantumalpha/logs/trade_trigger.log
StandardError=append:/home/qa/quantumalpha/logs/trade_trigger.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable qa-trade-trigger.service
sudo systemctl start qa-trade-trigger.service
sudo systemctl status qa-trade-trigger.service
```

### Step 9 — Commit and merge

```bash
git add bot/trade_trigger/ pytest.ini
git status  # review
git commit -m "feat(trade-trigger): foundation module — models, classifier, filters, bot, tests

- 31 event types mapped to QA-tradeable tickers (BTC/COPX/URA/KTOS excluded)
- Heuristic L1 + Claude L2 classifier
- Velocity, Corroboration, Anti-Bias filters (closes QA design specs)
- aiogram 3.27 standalone bot (Bot #2)
- SQLite persistence with WAL + foreign keys
- 60/60 pytest pass, including real Hormuz May 3 regression test
- systemd unit: qa-trade-trigger.service

Sources NOT YET CONNECTED — Phase 3 will add MT Newswires MCP + Polymarket
+ WhiteHouse/OFAC RSS feeds."

git push origin feature/trade-trigger
# After review: merge to main
```

---

## What's implemented (Phase 3)

- ✅ **Polymarket source** — `sources/polymarket.py` polls 8 default markets
  (Hormuz, Iran deal, Fed cuts, Ukraine, China-Taiwan, recession), detects
  ≥5pp odds shifts in 15-min window, emits synthetic NewsEvents
- ✅ **Pipeline orchestrator** — `pipeline.py` chains all 5 gates with
  full audit trail per event
- ✅ **Classifier extension** — heuristic rules now recognize Polymarket
  synthetic-event headlines

## What's NOT yet implemented (Phase 3.5+)

- **MT Newswires MCP integration** — institutional speed source
- **WhiteHouse.gov + OFAC + State Dept RSS feeds**
- **bot_runner integration with pipeline** — currently bot_runner is a
  passive skeleton; needs APScheduler tying PolymarketWatcher → pipeline
  → bot push (Phase 3.5, fast)
- **Inline button audit trail (`/tt_audit`):** stub returns "coming"
- **Backtest harness:** historical event replay engine. Phase 5.

## Polymarket configuration

`sources/polymarket.py:DEFAULT_WATCHLIST` — list of `MarketSpec` items.
Each item:

```python
MarketSpec(
    slug="will-iran-close-the-strait-of-hormuz-in-may-2026",
    outcome="Yes",
    event_type="hormuz_escalation",  # must be in trade_trigger_mapping
    direction="up",                  # 'up' | 'down' | 'either'
    shift_threshold=0.05,            # 5 percentage points
    label="Iran-Hormuz-closure",
)
```

To override: pass custom watchlist to `PolymarketWatcher(..., watchlist=[...])`.

**IMPORTANT:** Polymarket slugs change. Verify each slug at
`https://polymarket.com/event/<slug>` before relying on it. If a market
is no longer active, the watcher silently skips (logged at DEBUG level).

## Pipeline tuning knobs

`PipelineConfig`:

| Param | Default | Notes |
|---|---|---|
| `bucket_cap_pct` | 10.0 | Max % of bucket per trigger |
| `min_actionability_score` | 7.0 (L2) | Lower if running L1-only (no API) |
| `require_anti_bias` | True | Set False if BybitClient unavailable |
| `require_corroboration` | True | Set False for testing |
| `require_velocity` | True | |
| `log_audit_to_db` | True | Powers `/tt_audit` and postmortems |

---

## Running tests

```bash
cd /home/qa/quantumalpha
python -m pytest bot/trade_trigger/tests -v
python -m pytest bot/trade_trigger/tests -v -k hormuz  # specific
python -m pytest bot/trade_trigger/tests -v --tb=long  # full trace
```

---

## Closing QA design-spec gaps

This module concretely implements three previously-only-design specs:

| Design spec (PDF) | This module |
|---|---|
| Anti-Bias Gate | `filters/anti_bias_check.py` (live RSI + intraday) |
| Cross-source corroboration gate | `filters/corroboration_gate.py` |
| Signal #45 (Diplomatic Feed v1) | Replaces it with proper trade-trigger pipeline |

After successful Phase 3-4 deployment, these design specs can be marked
"production realized" in QA documentation.

---

## Author / Version

QuantumAlpha · v0.1.0 · 2026-05-06
