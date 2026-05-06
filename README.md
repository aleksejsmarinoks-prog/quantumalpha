# QuantumAlpha v2.3.2

Multi-agent trading bot for Bybit perpetuals, controlled via Telegram. Runs as a single asyncio process (`python -m bot.main`) and delegates to a risk-vetted strategy orchestra. Default mode is **paper**; live trading requires explicit env-flag opt-in after the validation phase passes.

> **Status (2026-05-06):** Paper-mode validation phase, day 8 of 30. Active capital $1,000 (live trading disabled). 0 fills executed to date — orchestra ticks every 5 min but no strategy has triggered an entry yet. See [Known issues](#known-issues) below.

---

## Architecture

```
bot/
├── main.py                 — entry point: wires components and starts polling
├── scheduler.py            — APScheduler driver (orchestra ticks, funding, macro)
├── core/
│   ├── bybit_client.py     — REST + WebSocket wrapper
│   ├── pnl_ledger.py       — SQLite ledger (data/pnl.db)
│   ├── risk_kernel.py      — DD limits, cooldown, kill-switch (vetoes every order)
│   ├── funding_monitor.py  — 5-min poll of BTC/ETH/SOL funding rates
│   ├── earn_manager.py     — Bybit Earn (Flexible + Fixed-Term + staking)
│   └── macro_events.py     — VIX / gold / oil watchers (yfinance)
├── strategies/
│   ├── base_strategy.py    — abstract base
│   ├── orchestra.py        — StrategyOrchestra: one tick / 5 min
│   ├── funding_arb.py      — delta-neutral funding harvest
│   ├── mean_reversion.py   — panic-dump entries
│   ├── cvd_divergence.py   — CVD vs. price divergence
│   └── dca_dips.py         — staged DCA on drawdowns
└── handlers/
    ├── trading_commands.py     — /balance /positions /halt /resume /paper_pnl ...
    └── strategy_commands.py    — /strategies /enable_strat /disable_strat ...
```

Wiring order (in `bot/main.py`): `BybitClient → PnLLedger → RiskKernel → FundingMonitor → EarnManager → strategies → StrategyOrchestra → aiogram routers → polling`.

**Capital model:** $1K active trading vs $25K passive Earn (Flexible Savings + Fixed-Term ladder + on-chain staking). The two layers share the `RiskKernel` veto but settle into separate ledger tables.

---

## Quick start (local)

```bash
git clone https://github.com/aleksejsmarinoks-prog/quantumalpha.git
cd quantumalpha
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — at minimum: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALLOWED_USER_ID
python -m bot.main
```

The bot runs in paper-mode with no Bybit keys (public endpoints only) — funding monitor and orchestra still tick.

---

## Deployment

Production runs on a single Hetzner CPX22 (Ubuntu 24.04, 2 vCPU, 4 GB RAM) under `systemd` as `qa-bot.service`. Full deploy + ops runbook in [`docs/DEPLOY.md`](docs/DEPLOY.md). Daily backup: see DEPLOY §9.

---

## Strategies

| Strategy | File | Status |
|---|---|---|
| `funding_arb` | `bot/strategies/funding_arb.py` | Enabled, no fills yet. Missing `get_strategy_id()` — see issues. |
| `mean_reversion` | `bot/strategies/mean_reversion.py` | Enabled, no fills yet. |
| `cvd_divergence` | `bot/strategies/cvd_divergence.py` | Enabled, no fills yet. Kline-dependent (degraded — see issues). |
| `dca_dips` | `bot/strategies/dca_dips.py` | Enabled, no fills yet. |

All four extend `base_strategy.BaseStrategy` and are coordinated by `StrategyOrchestra`. Per-strategy enable/disable via env (`STRATEGY_*_ENABLED`) or Telegram (`/enable_strat`, `/disable_strat`).

---

## Telegram commands

**Trading** (from `bot/handlers/trading_commands.py`):
- `/balance` — equity + unrealised PnL
- `/positions` — open positions
- `/funding` — current rates + 7d stats
- `/earn`, `/earn_add`, `/earn_plan` — Earn portfolio + planning
- `/paper_pnl` — paper-mode P&L summary
- `/risk_status` — DD/cooldown/halt state
- `/halt <hours> <reason>` — kill-switch
- `/resume` — clear halt

**Strategy** (from `bot/handlers/strategy_commands.py`):
- `/strategies` — list all
- `/strat_status <name>` — per-strategy state
- `/enable_strat <name>` / `/disable_strat <name>`
- `/halt_strat <name>` / `/resume_strat <name>`
- `/strat_positions <name>`
- `/orchestra` — orchestra-level summary

Access is gated to `ALLOWED_USER_ID` from `.env`.

---

## Current status (2026-05-06)

- **Mode:** paper (`LIVE_TRADING=false`, `QA_LIVE_EARN_MODE=false`, `BYBIT_TESTNET=true`)
- **Active capital:** $1,000 (validation budget)
- **Validation phase:** day 8 of 30
- **Trades executed:** 0
- **Funding history collected:** ~7 days across BTC/ETH/SOL (`data/funding.db`)
- **Service:** `qa-bot.service` running, restart=always, logs to `~/quantumalpha/logs/qa.log`

---

## Known issues

- **Zero fills.** The orchestra has been ticking every 5 min for ~7 days without any strategy generating an entry. Either thresholds are too tight for current market conditions, or there is a logic bug in `StrategyOrchestra.tick`. Needs investigation before going live.
- **`FundingArbStrategy.get_strategy_id` missing.** The other three strategies define this method; `funding_arb.py` does not. Likely raises `AttributeError` on any orchestra path that calls it.
- **`BybitClient.get_klines` missing.** Every 5-min tick logs `WARNING qa.scheduler: Market data fetch error for {SYMBOL}: 'BybitClient' object has no attribute 'get_klines'` for BTCUSDT/ETHUSDT/SOLUSDT. `cvd_divergence` (kline-dependent) silently degrades.
- **Auth-required scheduler jobs failing.** `portfolio_sync` and `equity_snapshot` log `RuntimeError: Auth required but BYBIT_API_KEY/BYBIT_API_SECRET not set` every cycle. Expected while running keyless in paper-mode, but the jobs should short-circuit instead of throwing.
- **Suspected double logging.** Both `StandardOutput` and `StandardError` append to the same `qa.log` via systemd; combined with Python's `logging` setup, some lines may appear twice. Not yet confirmed.

---

## License

Private. All rights reserved.
