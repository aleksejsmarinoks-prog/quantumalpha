# QuantumAlpha

Multi-agent investment system: Bybit prop trading + macro pipeline + Telegram command center.

**Status:** Active development. Phase 1: Bybit prop trading foundation (paper-mode).

---

## Architecture (high-level)

```
┌─────────────────────────────────────────────────────────────────┐
│                       RISK KERNEL (veto)                         │
│  Hard limits: daily/weekly/total DD · cooldown · anti-pattern    │
└──────────────────┬──────────────────────────────────────────────┘
                   │
         ┌─────────┴─────────┐
         ▼                   ▼
   ┌───────────┐       ┌──────────┐
   │  ACTIVE   │       │ PASSIVE  │
   │  TRADING  │       │  EARN    │
   │  ($1K)    │       │  ($25K)  │
   └─────┬─────┘       └────┬─────┘
         │                  │
         ▼                  ▼
   funding arb         Flexible Savings
   basis trade         Fixed-Term ladder
   mean reversion      On-Chain Staking
         │                  │
         └────────┬─────────┘
                  ▼
         ┌────────────────┐
         │  PnL LEDGER    │
         │  (SQLite)      │
         └────────┬───────┘
                  ▼
         ┌────────────────┐
         │ TELEGRAM BOT   │
         └────────────────┘
```

## Project structure

```
quantumalpha/
├── bot/
│   ├── core/
│   │   ├── risk_kernel.py        — hard limits + kill switches
│   │   ├── pnl_ledger.py         — SQLite transaction log
│   │   ├── bybit_client.py       — REST + WebSocket wrapper
│   │   ├── market_data.py        — macro snapshot (legacy)
│   │   └── scheduler.py          — APScheduler driver
│   ├── strategies/
│   │   ├── funding_arb.py        — (P1) delta-neutral funding harvest
│   │   ├── basis_trade.py        — (P2) calendar spread
│   │   └── mean_reversion.py     — (P2) panic dump entries
│   ├── handlers/
│   │   ├── commands.py           — Telegram slash commands
│   │   ├── callbacks.py          — Inline button callbacks
│   │   └── trading_commands.py   — (new) prop trading controls
│   ├── reports/
│   │   ├── pdf_generator.py      — macro brief PDFs
│   │   └── equity_tracker.py     — (new) Google Sheets export
│   ├── bot.py                    — entry point
│   ├── quantforge.py             — tactical execution agent
│   ├── qa_bridge.py              — QA → QuantForge bridge
│   └── chronos_backtester.py     — strategy backtester
├── research/                     — DeepSeek research outputs
├── docs/
│   └── DECISION_LOG.md           — architectural decisions
├── data/                         — runtime state (gitignored)
├── deploy.sh
├── requirements.txt
└── .env.example
```

## Quick test (no API keys needed)

```bash
# Risk kernel smoke test
python bot/core/risk_kernel.py

# PnL ledger smoke test
python bot/core/pnl_ledger.py

# Bybit public API smoke test
python bot/core/bybit_client.py
```

## Production deploy (Hetzner CX22, Ubuntu 24.04)

```bash
git clone <this-repo> /opt/qa_bot
cd /opt/qa_bot
cp .env.example .env
# Edit .env: BOT_TOKEN, ALLOWED_USER_ID, BYBIT_API_KEY, BYBIT_API_SECRET
bash deploy.sh
systemctl start qa_bot
journalctl -u qa_bot -f
```

## Capital allocation ($63K total on Bybit)

| Layer | Amount | Purpose |
|---|---|---|
| Active trading | $1,000 | Bot validation phase 1 (paper → live) |
| Passive Earn | $25,000 | Anti-inflation, blended ~7-9% APR |
| Strategic hold | $37,500 | Available for scale-up after validation |

## Scaling rule

Increase active trading capital **only** when:
- Sharpe ≥ 1.5 over rolling 30 days
- Max drawdown ≤ 8%
- Profitable in 3+ of 4 weeks

Halt trading and full review on any single-month DD > 15%.

## License

Private. All rights reserved.
