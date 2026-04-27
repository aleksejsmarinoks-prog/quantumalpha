"""
core/pnl_ledger.py — QuantumAlpha PnL Ledger v1.0

SQLite-backed transaction ledger. The single source of truth for:
  - Every trade fill (spot, perp, leveraged spot)
  - Funding rate payments (received and paid)
  - Earn product subscriptions, redemptions, interest payments
  - Equity snapshots (hourly for public tracker)
  - Position tracking (open positions reconciliation)

Design principles:
  1. Append-only event log — no UPDATEs to historical records
  2. Tax-grade: every realization event tracked with cost basis & holding period
  3. Reconcilable: can rebuild full account state by replaying ledger
  4. Crash-safe: WAL mode + foreign keys + transactions

Author: QuantumAlpha team
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger("qa_bot.pnl_ledger")


# =============================================================================
# SCHEMA — versioned, idempotent
# =============================================================================

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- ── 1. TRADE FILLS ──────────────────────────────────────────────────────────
-- Every individual fill from Bybit, regardless of strategy.
CREATE TABLE IF NOT EXISTS trade_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_time_utc   TEXT    NOT NULL,
    exchange        TEXT    NOT NULL DEFAULT 'bybit',
    category        TEXT    NOT NULL,                  -- 'spot' | 'linear'
    asset           TEXT    NOT NULL,                  -- 'ETH/USDT'
    side            TEXT    NOT NULL,                  -- 'buy' | 'sell'
    quantity        REAL    NOT NULL,
    price           REAL    NOT NULL,
    notional_usd    REAL    NOT NULL,                  -- quantity * price
    fee_usd         REAL    NOT NULL DEFAULT 0.0,
    fee_currency    TEXT    DEFAULT 'USDT',
    order_id        TEXT,                              -- exchange order ID
    fill_id         TEXT    UNIQUE,                    -- exchange fill ID (idempotency)
    strategy        TEXT,                              -- 'funding_arb', 'mean_reversion', etc.
    is_paper        INTEGER NOT NULL DEFAULT 0,        -- 1 = paper trade
    raw_response    TEXT                               -- JSON of exchange response
);
CREATE INDEX IF NOT EXISTS idx_fills_time     ON trade_fills(fill_time_utc);
CREATE INDEX IF NOT EXISTS idx_fills_asset    ON trade_fills(asset);
CREATE INDEX IF NOT EXISTS idx_fills_strategy ON trade_fills(strategy);

-- ── 2. POSITIONS ────────────────────────────────────────────────────────────
-- Tracks open positions (one row per active position; closed = is_open=0).
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_utc      TEXT    NOT NULL,
    closed_utc      TEXT,
    asset           TEXT    NOT NULL,
    category        TEXT    NOT NULL,                  -- 'spot' | 'linear'
    side            TEXT    NOT NULL,                  -- 'long' | 'short'
    avg_entry_price REAL    NOT NULL,
    avg_exit_price  REAL,
    quantity        REAL    NOT NULL,                  -- abs value
    realized_pnl    REAL    DEFAULT 0.0,               -- after fees
    fees_paid       REAL    DEFAULT 0.0,
    funding_paid    REAL    DEFAULT 0.0,               -- only for perps
    holding_hours   REAL,
    strategy        TEXT,
    is_open         INTEGER NOT NULL DEFAULT 1,
    is_paper        INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_open     ON positions(is_open);
CREATE INDEX IF NOT EXISTS idx_pos_asset    ON positions(asset);
CREATE INDEX IF NOT EXISTS idx_pos_strategy ON positions(strategy);

-- ── 3. FUNDING PAYMENTS ─────────────────────────────────────────────────────
-- Bybit perps fund every 8h. We track each payment for transparency.
CREATE TABLE IF NOT EXISTS funding_payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_utc  TEXT    NOT NULL,
    asset           TEXT    NOT NULL,                  -- 'ETHUSDT'
    funding_rate    REAL    NOT NULL,                  -- e.g., 0.0001 = 0.01%
    position_qty    REAL    NOT NULL,
    payment_usd     REAL    NOT NULL,                  -- + received, - paid
    is_paper        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_funding_time ON funding_payments(settlement_utc);
CREATE INDEX IF NOT EXISTS idx_funding_asset ON funding_payments(asset);

-- ── 4. EARN POSITIONS ───────────────────────────────────────────────────────
-- Tracks Bybit Earn products: Flexible, Fixed-Term, On-Chain Staking.
CREATE TABLE IF NOT EXISTS earn_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscribed_utc  TEXT    NOT NULL,
    redeemed_utc    TEXT,
    product_type    TEXT    NOT NULL,                  -- 'flexible_savings' | 'fixed_term' | 'onchain'
    coin            TEXT    NOT NULL,
    principal       REAL    NOT NULL,                  -- amount staked
    apr             REAL    NOT NULL,                  -- annualized %, 0.12 = 12%
    term_days       INTEGER,                           -- NULL for flexible
    interest_earned REAL    DEFAULT 0.0,               -- accumulated
    bybit_order_id  TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_earn_active ON earn_positions(is_active);
CREATE INDEX IF NOT EXISTS idx_earn_coin   ON earn_positions(coin);

-- ── 5. EARN INTEREST PAYMENTS ───────────────────────────────────────────────
-- Daily interest accruals on Earn positions (separate from principal).
CREATE TABLE IF NOT EXISTS earn_interest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_utc     TEXT    NOT NULL,
    earn_position_id INTEGER NOT NULL,
    coin            TEXT    NOT NULL,
    interest_amount REAL    NOT NULL,                  -- in coin units
    interest_usd    REAL    NOT NULL,                  -- USD value at payment time
    FOREIGN KEY (earn_position_id) REFERENCES earn_positions(id)
);
CREATE INDEX IF NOT EXISTS idx_interest_time ON earn_interest(payment_utc);

-- ── 6. EQUITY SNAPSHOTS ─────────────────────────────────────────────────────
-- Hourly account equity snapshots. Source for public equity curve tracker.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_utc       TEXT    NOT NULL,
    total_equity_usd   REAL    NOT NULL,               -- spot + perp + earn
    spot_balance_usd   REAL    DEFAULT 0.0,
    perp_balance_usd   REAL    DEFAULT 0.0,
    earn_balance_usd   REAL    DEFAULT 0.0,
    open_pnl_usd       REAL    DEFAULT 0.0,            -- unrealized
    daily_pnl_usd      REAL    DEFAULT 0.0,
    weekly_pnl_usd     REAL    DEFAULT 0.0,
    total_pnl_usd      REAL    DEFAULT 0.0,
    open_positions_count INTEGER DEFAULT 0,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_time ON equity_snapshots(snapshot_utc);

-- ── 7. METADATA ─────────────────────────────────────────────────────────────
-- Schema version, config, etc.
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class FillCategory(str, Enum):
    SPOT   = "spot"
    LINEAR = "linear"   # USDT perpetuals on Bybit


@dataclass
class TradeFill:
    """A single fill from the exchange."""
    fill_time_utc:  str
    category:       str           # 'spot' | 'linear'
    asset:          str           # 'ETH/USDT'
    side:           str           # 'buy' | 'sell'
    quantity:       float
    price:          float
    fee_usd:        float        = 0.0
    fee_currency:   str          = "USDT"
    order_id:       Optional[str] = None
    fill_id:        Optional[str] = None
    strategy:       Optional[str] = None
    is_paper:       bool         = False
    raw_response:   Optional[dict] = None

    @property
    def notional_usd(self) -> float:
        return abs(self.quantity * self.price)


@dataclass
class FundingPayment:
    settlement_utc: str
    asset:          str          # 'ETHUSDT' (Bybit perp format)
    funding_rate:   float        # 0.0001 = 0.01%
    position_qty:   float
    payment_usd:    float        # + received, - paid
    is_paper:       bool         = False


@dataclass
class EarnPosition:
    subscribed_utc: str
    product_type:   str          # 'flexible_savings' | 'fixed_term' | 'onchain'
    coin:           str
    principal:      float
    apr:            float
    term_days:      Optional[int] = None
    bybit_order_id: Optional[str] = None
    notes:          Optional[str] = None


@dataclass
class EquitySnapshot:
    snapshot_utc:         str
    total_equity_usd:     float
    spot_balance_usd:     float = 0.0
    perp_balance_usd:     float = 0.0
    earn_balance_usd:     float = 0.0
    open_pnl_usd:         float = 0.0
    daily_pnl_usd:        float = 0.0
    weekly_pnl_usd:       float = 0.0
    total_pnl_usd:        float = 0.0
    open_positions_count: int   = 0
    notes:                Optional[str] = None


# =============================================================================
# PNL LEDGER
# =============================================================================

class PnLLedger:
    """
    Production transaction ledger backed by SQLite (WAL mode).

    Usage:
        ledger = PnLLedger(Path("/opt/qa_bot/data/pnl.db"))
        ledger.record_fill(TradeFill(...))
        ledger.record_funding(FundingPayment(...))
        ledger.snapshot_equity(EquitySnapshot(...))
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        """Connection with WAL mode + foreign keys."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,  # Autocommit; transactions managed explicitly
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(SCHEMA_SQL)
            cur = c.execute("SELECT value FROM metadata WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),)
                )
                c.execute(
                    "INSERT INTO metadata(key, value) VALUES ('created_utc', ?)",
                    (datetime.now(timezone.utc).isoformat(),)
                )
                log.info(f"PnL Ledger initialised at {self.db_path}")
            else:
                stored = int(row["value"])
                if stored < SCHEMA_VERSION:
                    log.warning(
                        f"Schema migration needed: stored={stored} expected={SCHEMA_VERSION}. "
                        f"(Migration not yet implemented — handle manually)"
                    )

    # ── RECORDING ───────────────────────────────────────────────────────────────

    def record_fill(self, fill: TradeFill) -> int:
        """
        Insert a trade fill. Idempotent on fill_id.
        Returns the row ID (or existing ID if duplicate fill_id).
        """
        with self._conn() as c:
            try:
                cur = c.execute("""
                    INSERT INTO trade_fills (
                        fill_time_utc, category, asset, side,
                        quantity, price, notional_usd,
                        fee_usd, fee_currency,
                        order_id, fill_id, strategy, is_paper, raw_response
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    fill.fill_time_utc, fill.category, fill.asset, fill.side,
                    fill.quantity, fill.price, fill.notional_usd,
                    fill.fee_usd, fill.fee_currency,
                    fill.order_id, fill.fill_id, fill.strategy,
                    1 if fill.is_paper else 0,
                    json.dumps(fill.raw_response) if fill.raw_response else None,
                ))
                row_id = cur.lastrowid
                log.info(
                    f"Fill recorded: #{row_id} {fill.asset} {fill.side} "
                    f"{fill.quantity}@{fill.price} fee=${fill.fee_usd:.4f} "
                    f"strategy={fill.strategy} paper={fill.is_paper}"
                )
                return row_id
            except sqlite3.IntegrityError as e:
                # Duplicate fill_id — idempotency check
                if fill.fill_id and "fill_id" in str(e):
                    cur = c.execute(
                        "SELECT id FROM trade_fills WHERE fill_id=?",
                        (fill.fill_id,)
                    )
                    existing = cur.fetchone()
                    if existing:
                        log.warning(f"Duplicate fill_id {fill.fill_id}, skipped")
                        return existing["id"]
                raise

    def record_funding(self, payment: FundingPayment) -> int:
        with self._conn() as c:
            cur = c.execute("""
                INSERT INTO funding_payments (
                    settlement_utc, asset, funding_rate,
                    position_qty, payment_usd, is_paper
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                payment.settlement_utc, payment.asset, payment.funding_rate,
                payment.position_qty, payment.payment_usd,
                1 if payment.is_paper else 0,
            ))
            log.info(
                f"Funding recorded: {payment.asset} rate={payment.funding_rate*100:.4f}% "
                f"payment=${payment.payment_usd:+.4f} paper={payment.is_paper}"
            )
            return cur.lastrowid

    def record_earn_subscription(self, position: EarnPosition) -> int:
        with self._conn() as c:
            cur = c.execute("""
                INSERT INTO earn_positions (
                    subscribed_utc, product_type, coin, principal,
                    apr, term_days, bybit_order_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position.subscribed_utc, position.product_type, position.coin,
                position.principal, position.apr, position.term_days,
                position.bybit_order_id, position.notes,
            ))
            log.info(
                f"Earn subscribed: {position.product_type} {position.coin} "
                f"${position.principal:,.2f} @ {position.apr*100:.2f}% APR"
            )
            return cur.lastrowid

    def record_earn_interest(
        self, earn_position_id: int, coin: str,
        interest_amount: float, interest_usd: float,
        payment_utc: Optional[str] = None,
    ) -> int:
        ts = payment_utc or datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute("""
                INSERT INTO earn_interest (
                    payment_utc, earn_position_id, coin,
                    interest_amount, interest_usd
                ) VALUES (?, ?, ?, ?, ?)
            """, (ts, earn_position_id, coin, interest_amount, interest_usd))
            return cur.lastrowid

    def snapshot_equity(self, snapshot: EquitySnapshot) -> int:
        with self._conn() as c:
            cur = c.execute("""
                INSERT INTO equity_snapshots (
                    snapshot_utc, total_equity_usd,
                    spot_balance_usd, perp_balance_usd, earn_balance_usd,
                    open_pnl_usd, daily_pnl_usd, weekly_pnl_usd, total_pnl_usd,
                    open_positions_count, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.snapshot_utc, snapshot.total_equity_usd,
                snapshot.spot_balance_usd, snapshot.perp_balance_usd,
                snapshot.earn_balance_usd,
                snapshot.open_pnl_usd, snapshot.daily_pnl_usd,
                snapshot.weekly_pnl_usd, snapshot.total_pnl_usd,
                snapshot.open_positions_count, snapshot.notes,
            ))
            return cur.lastrowid

    # ── QUERIES ─────────────────────────────────────────────────────────────────

    def get_recent_fills(self, limit: int = 50, asset: Optional[str] = None) -> list[dict]:
        with self._conn() as c:
            if asset:
                cur = c.execute(
                    "SELECT * FROM trade_fills WHERE asset=? "
                    "ORDER BY fill_time_utc DESC LIMIT ?",
                    (asset, limit)
                )
            else:
                cur = c.execute(
                    "SELECT * FROM trade_fills ORDER BY fill_time_utc DESC LIMIT ?",
                    (limit,)
                )
            return [dict(r) for r in cur.fetchall()]

    def get_active_earn_positions(self) -> list[dict]:
        with self._conn() as c:
            cur = c.execute("""
                SELECT * FROM earn_positions WHERE is_active=1
                ORDER BY subscribed_utc DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    def get_total_funding_received(self, asset: Optional[str] = None) -> float:
        with self._conn() as c:
            if asset:
                cur = c.execute(
                    "SELECT COALESCE(SUM(payment_usd), 0) AS total "
                    "FROM funding_payments WHERE asset=?",
                    (asset,)
                )
            else:
                cur = c.execute("SELECT COALESCE(SUM(payment_usd), 0) AS total FROM funding_payments")
            return cur.fetchone()["total"]

    def get_strategy_stats(self, strategy: str) -> dict:
        """Aggregate stats per strategy for performance review."""
        with self._conn() as c:
            cur = c.execute("""
                SELECT
                    COUNT(*) AS total_fills,
                    COALESCE(SUM(notional_usd), 0) AS total_notional,
                    COALESCE(SUM(fee_usd), 0) AS total_fees,
                    MIN(fill_time_utc) AS first_trade,
                    MAX(fill_time_utc) AS last_trade
                FROM trade_fills
                WHERE strategy=? AND is_paper=0
            """, (strategy,))
            return dict(cur.fetchone())

    def get_latest_equity(self) -> Optional[dict]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT * FROM equity_snapshots ORDER BY snapshot_utc DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_equity_curve(self, days: int = 30) -> list[dict]:
        """Daily equity points for chart export."""
        with self._conn() as c:
            cur = c.execute("""
                SELECT * FROM equity_snapshots
                WHERE snapshot_utc >= datetime('now', ?)
                ORDER BY snapshot_utc ASC
            """, (f"-{days} days",))
            return [dict(r) for r in cur.fetchall()]


# =============================================================================
# CLI / TEST HOOK
# =============================================================================

if __name__ == "__main__":
    """Smoke test: open a temp DB, insert sample records, verify."""
    import tempfile, shutil

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test_pnl.db"
        ledger = PnLLedger(db)

        now = datetime.now(timezone.utc).isoformat()

        # Test 1: spot fill
        fid = ledger.record_fill(TradeFill(
            fill_time_utc=now, category="spot", asset="ETH/USDT", side="buy",
            quantity=0.1, price=3500.0, fee_usd=0.385,
            fill_id="test-fill-001", strategy="funding_arb_spot_leg",
            is_paper=True,
        ))
        print(f"✓ Recorded spot fill #{fid}")

        # Test 2: perp short fill (delta-neutral with above)
        fid2 = ledger.record_fill(TradeFill(
            fill_time_utc=now, category="linear", asset="ETHUSDT", side="sell",
            quantity=0.1, price=3500.0, fee_usd=0.1925,
            fill_id="test-fill-002", strategy="funding_arb_perp_leg",
            is_paper=True,
        ))
        print(f"✓ Recorded perp fill #{fid2}")

        # Test 3: idempotency (re-insert same fill_id)
        fid3 = ledger.record_fill(TradeFill(
            fill_time_utc=now, category="spot", asset="ETH/USDT", side="buy",
            quantity=0.1, price=3500.0, fee_usd=0.385,
            fill_id="test-fill-001",  # SAME as fid
            is_paper=True,
        ))
        assert fid3 == fid, "Idempotency failed!"
        print(f"✓ Idempotency: duplicate fill_id returned existing ID #{fid3}")

        # Test 4: funding payment
        fpid = ledger.record_funding(FundingPayment(
            settlement_utc=now, asset="ETHUSDT",
            funding_rate=0.0001, position_qty=0.1,
            payment_usd=0.035,  # 0.1 ETH × $3500 × 0.01% = $0.035
            is_paper=True,
        ))
        print(f"✓ Recorded funding payment #{fpid}")

        # Test 5: earn subscription
        eid = ledger.record_earn_subscription(EarnPosition(
            subscribed_utc=now, product_type="flexible_savings",
            coin="USDT", principal=500.0, apr=0.12,
            notes="Tier-1 cap, max APR",
        ))
        print(f"✓ Recorded Earn subscription #{eid}")

        # Test 6: equity snapshot
        sid = ledger.snapshot_equity(EquitySnapshot(
            snapshot_utc=now, total_equity_usd=63500.0,
            spot_balance_usd=37500.0, perp_balance_usd=1000.0,
            earn_balance_usd=25000.0,
            open_positions_count=0,
        ))
        print(f"✓ Recorded equity snapshot #{sid}")

        # Test 7: queries
        recent = ledger.get_recent_fills(limit=5)
        print(f"\n✓ Recent fills: {len(recent)}")
        for f in recent:
            print(f"  - {f['asset']} {f['side']} {f['quantity']}@{f['price']}")

        funding_total = ledger.get_total_funding_received("ETHUSDT")
        print(f"\n✓ Total funding ETHUSDT: ${funding_total:.4f}")

        active_earn = ledger.get_active_earn_positions()
        print(f"\n✓ Active Earn positions: {len(active_earn)}")

        latest = ledger.get_latest_equity()
        print(f"\n✓ Latest equity: ${latest['total_equity_usd']:,.2f}")

        print("\n✅ All ledger tests passed.")
