"""
Trade journal + state persistence: SQLite database.
Survives bot restarts. Used for audits, cooldowns, and forensics.
"""

import sqlite3
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

log = logging.getLogger("db")

DB_PATH = Path(__file__).parent / "trading.db"


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Initialize database schema. Safe to call multiple times."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ─────────────────────── Trade Journal ───────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,  -- 'BUY' or 'SELL'
            qty REAL NOT NULL,
            price REAL NOT NULL,
            timestamp_utc TEXT NOT NULL,  -- ISO 8601 UTC
            reason TEXT,  -- e.g. 'ENTRY_SIGNAL', 'TAKE_PROFIT', 'STOP_LOSS', 'STALE', 'FIB_TP', 'FIB_SL'
            signal_score INTEGER,  -- entry signal strength (0-5)
            bars_source TEXT,  -- 'iex' | 'sip' | 'yfinance' | 'fallback'
            pnl_pct REAL,  -- realized P&L % (on exit only)
            order_id TEXT UNIQUE,  -- Alpaca order ID
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_symbol_time ON trades(symbol, timestamp_utc DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS ix_reason ON trades(reason)")

    # ─────────────────────── Stop-Loss Cooldown ───────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            symbol TEXT PRIMARY KEY,
            cooldown_until_utc TEXT NOT NULL,  -- ISO 8601 UTC when cooldown expires
            reason TEXT,  -- e.g. 'STOP_LOSS', 'FIB_SL'
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ─────────────────────── Daily State (for reconciliation) ───────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_state (
            date TEXT PRIMARY KEY,  -- YYYY-MM-DD (ET)
            equity_start REAL,
            equity_end REAL,
            cash_start REAL,
            cash_end REAL,
            daytrades_used INTEGER,
            num_entries INTEGER,
            num_exits INTEGER,
            total_pnl_realized REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    log.info(f"Database initialized: {db_path}")
    return conn


def get_db_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Get or create database connection."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────── Trade Journal ───────────────────────

def log_trade(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    reason: str,
    signal_score: Optional[int] = None,
    bars_source: Optional[str] = None,
    pnl_pct: Optional[float] = None,
    order_id: Optional[str] = None,
) -> None:
    """Append a trade to the journal."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (symbol, side, qty, price, timestamp_utc, reason, signal_score, bars_source, pnl_pct, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            side,
            qty,
            price,
            datetime.now(timezone.utc).isoformat(),
            reason,
            signal_score,
            bars_source,
            pnl_pct,
            order_id,
        ))
        conn.commit()
        conn.close()
        log.debug(f"Logged trade: {symbol} {side} {qty}@{price} ({reason})")
    except Exception as e:
        log.error(f"Failed to log trade: {e}")


def get_today_trades(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all trades from today (UTC)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if symbol:
            cursor.execute("""
                SELECT * FROM trades
                WHERE date(timestamp_utc) = date('now')
                AND symbol = ?
                ORDER BY timestamp_utc DESC
            """, (symbol,))
        else:
            cursor.execute("""
                SELECT * FROM trades
                WHERE date(timestamp_utc) = date('now')
                ORDER BY timestamp_utc DESC
            """)

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Failed to fetch today's trades: {e}")
        return []


def get_trade_stats(days: int = 7) -> Dict[str, Any]:
    """Get trading stats for the last N days."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) as buys,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) as sells,
                AVG(pnl_pct) as avg_pnl_pct,
                COUNT(DISTINCT symbol) as unique_symbols,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl_pct < 0 THEN 1 ELSE 0 END) as losing_trades
            FROM trades
            WHERE datetime(timestamp_utc) >= datetime('now', '-{days} days')
        """)

        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as e:
        log.error(f"Failed to fetch trade stats: {e}")
        return {}


# ─────────────────────── Stop-Loss Cooldown ───────────────────────

def set_cooldown(symbol: str, cooldown_until_utc: str, reason: str = "STOP_LOSS") -> None:
    """Set cooldown for a symbol (prevents re-entry for N minutes)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO cooldowns (symbol, cooldown_until_utc, reason)
            VALUES (?, ?, ?)
        """, (symbol, cooldown_until_utc, reason))
        conn.commit()
        conn.close()
        log.info(f"Set cooldown: {symbol} until {cooldown_until_utc}")
    except Exception as e:
        log.error(f"Failed to set cooldown: {e}")


def is_in_cooldown(symbol: str, now_utc: datetime) -> bool:
    """Check if symbol is currently in cooldown."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cooldown_until_utc FROM cooldowns WHERE symbol = ?
        """, (symbol,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return False

        cooldown_until = datetime.fromisoformat(row[0])
        is_cool = now_utc < cooldown_until
        if is_cool:
            log.debug(f"{symbol} still in cooldown until {cooldown_until}")
        return is_cool
    except Exception as e:
        log.error(f"Failed to check cooldown: {e}")
        return False


def clear_cooldown(symbol: str) -> None:
    """Clear cooldown for a symbol (allow re-entry)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cooldowns WHERE symbol = ?", (symbol,))
        conn.commit()
        conn.close()
        log.info(f"Cleared cooldown: {symbol}")
    except Exception as e:
        log.error(f"Failed to clear cooldown: {e}")


def get_active_cooldowns() -> Dict[str, str]:
    """Get all active cooldowns."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, cooldown_until_utc FROM cooldowns")
        rows = cursor.fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        log.error(f"Failed to fetch cooldowns: {e}")
        return {}


# ─────────────────────── Daily State ───────────────────────

def record_daily_state(
    date: str,  # YYYY-MM-DD ET
    equity_start: float,
    equity_end: float,
    cash_start: float,
    cash_end: float,
    daytrades_used: int,
    num_entries: int,
    num_exits: int,
    total_pnl_realized: float,
) -> None:
    """Record end-of-day state for reconciliation."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO daily_state
            (date, equity_start, equity_end, cash_start, cash_end, daytrades_used, num_entries, num_exits, total_pnl_realized)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, equity_start, equity_end, cash_start, cash_end, daytrades_used, num_entries, num_exits, total_pnl_realized))
        conn.commit()
        conn.close()
        log.info(f"Recorded daily state for {date}")
    except Exception as e:
        log.error(f"Failed to record daily state: {e}")


if __name__ == "__main__":
    # Test: initialize database
    logging.basicConfig(level=logging.INFO)
    init_db()
    log.info("✓ Database initialized successfully")
