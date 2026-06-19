"""
Trend-Investorr: PostgreSQL Database Manager

Connection pooling, query execution, transaction management,
and automatic schema migration.

Architecture:
    - psycopg2 connection pool (min 2, max 10 connections)
    - Context-manager-based transactions (auto-commit/rollback)
    - Parameterized queries only (SQL injection prevention)
    - Auto-retry on transient connection errors
    - Health checks and pool monitoring

Usage:
    db = Database(settings)
    db.connect()

    # Simple query
    rows = db.fetch_all("SELECT * FROM stocks WHERE sector = %s", ("IT",))

    # Transaction
    with db.transaction() as cur:
        cur.execute("INSERT INTO stocks ...", values)
        cur.execute("UPDATE app_config ...", values)
    # Auto-commits on exit, auto-rollback on exception

Raises:
    DatabaseError: On connection failure, query errors, pool exhaustion.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool
from loguru import logger

from core.exceptions import DatabaseError


class Database:
    """
    PostgreSQL database manager with connection pooling.

    Thread-safe: psycopg2's ThreadedConnectionPool handles
    concurrent access from multiple threads.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "trend_investorr",
        user: str = "postgres",
        password: str = "",
        min_connections: int = 2,
        max_connections: int = 10,
    ):
        self._host = host
        self._port = port
        self._dbname = dbname
        self._user = user
        self._password = password
        self._min_conn = min_connections
        self._max_conn = max_connections

        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._is_connected = False

    def connect(self) -> None:
        """
        Initialize the connection pool.

        Raises:
            DatabaseError: If connection fails after retries.
        """
        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                self._pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=self._min_conn,
                    maxconn=self._max_conn,
                    host=self._host,
                    port=self._port,
                    dbname=self._dbname,
                    user=self._user,
                    password=self._password,
                    connect_timeout=10,
                    options="-c timezone=Asia/Kolkata",
                )

                # Test connection
                conn = self._pool.getconn()
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                self._pool.putconn(conn)

                self._is_connected = True
                logger.info(
                    f"Database connected: {self._dbname}@{self._host}:{self._port} | "
                    f"pool={self._min_conn}-{self._max_conn}"
                )
                return

            except psycopg2.OperationalError as e:
                last_error = e
                logger.warning(f"DB connection failed (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)

            except Exception as e:
                raise DatabaseError(
                    f"Unexpected database error: {e}",
                    details={"host": self._host, "port": self._port, "db": self._dbname},
                )

        raise DatabaseError(
            f"Failed to connect to database after {max_retries} attempts: {last_error}",
            details={"host": self._host, "port": self._port, "db": self._dbname},
        )

    def disconnect(self) -> None:
        """Close all connections in the pool."""
        if self._pool:
            try:
                self._pool.closeall()
                self._is_connected = False
                logger.info("Database pool closed")
            except Exception as e:
                logger.warning(f"Error closing database pool: {e}")

    @contextmanager
    def get_connection(self) -> Generator[Any, None, None]:
        """
        Get a connection from the pool (context manager).

        Usage:
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(...)
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            yield conn
        except psycopg2.Error as e:
            raise DatabaseError(
                f"Database connection error: {e}",
                details={"error": str(e)},
            )
        finally:
            if conn:
                self._pool.putconn(conn)

    @contextmanager
    def transaction(self) -> Generator[Any, None, None]:
        """
        Execute queries within a transaction (auto-commit/rollback).

        Usage:
            with db.transaction() as cur:
                cur.execute("INSERT INTO ...", values)
                cur.execute("UPDATE ...", values)
            # Commits on success, rollbacks on exception
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = False
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            yield cur
            conn.commit()
        except psycopg2.Error as e:
            if conn:
                conn.rollback()
            raise DatabaseError(
                f"Transaction failed: {e}",
                details={"error": str(e)},
            )
        except Exception as e:
            if conn:
                conn.rollback()
            raise DatabaseError(
                f"Transaction error: {e}",
                details={"error": type(e).__name__, "message": str(e)},
            )
        finally:
            if conn:
                conn.autocommit = True
                self._pool.putconn(conn)

    # ── Query Helpers ────────────────────────────────────

    def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        """
        Execute a query and return a single row as a dict.

        Returns None if no rows found.

        Raises:
            DatabaseError: On query failure.
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                return dict(row) if row else None
        except psycopg2.Error as e:
            raise DatabaseError(
                f"Query failed: {e}",
                details={"query": query[:200], "error": str(e)},
            )
        finally:
            if conn:
                self._pool.putconn(conn)

    def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        """
        Execute a query and return all rows as a list of dicts.

        Raises:
            DatabaseError: On query failure.
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        except psycopg2.Error as e:
            raise DatabaseError(
                f"Query failed: {e}",
                details={"query": query[:200], "error": str(e)},
            )
        finally:
            if conn:
                self._pool.putconn(conn)

    def execute(self, query: str, params: tuple = ()) -> int:
        """
        Execute a non-SELECT query (INSERT, UPDATE, DELETE).

        Returns:
            Number of affected rows.

        Raises:
            DatabaseError: On query failure.
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.rowcount
        except psycopg2.Error as e:
            raise DatabaseError(
                f"Execute failed: {e}",
                details={"query": query[:200], "error": str(e)},
            )
        finally:
            if conn:
                self._pool.putconn(conn)

    def execute_returning(self, query: str, params: tuple = ()) -> dict | None:
        """
        Execute an INSERT/UPDATE with RETURNING clause.

        Returns:
            The returned row as a dict, or None.
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                return dict(row) if row else None
        except psycopg2.Error as e:
            raise DatabaseError(
                f"Execute returning failed: {e}",
                details={"query": query[:200], "error": str(e)},
            )
        finally:
            if conn:
                self._pool.putconn(conn)

    def execute_many(self, query: str, params_list: list[tuple]) -> int:
        """
        Execute a query for multiple parameter sets (batch insert).

        Returns:
            Total number of affected rows.
        """
        self._ensure_connected()
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.executemany(query, params_list)
                affected = cur.rowcount
            conn.commit()
            return affected
        except psycopg2.Error as e:
            if conn:
                conn.rollback()
            raise DatabaseError(
                f"Batch execute failed: {e}",
                details={"query": query[:200], "batch_size": len(params_list)},
            )
        finally:
            if conn:
                conn.autocommit = True
                self._pool.putconn(conn)

    # ── Schema Management ────────────────────────────────

    def run_migrations(self) -> None:
        """
        Run all schema migrations.

        Creates tables if they don't exist, applies incremental changes.
        Safe to run multiple times (idempotent via IF NOT EXISTS).
        """
        logger.info("Running database migrations...")
        try:
            with self.transaction() as cur:
                for statement in SCHEMA_MIGRATIONS:
                    cur.execute(statement)
            logger.info("Database migrations completed successfully")
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(
                f"Migration failed: {e}",
                details={"error": str(e)},
            )

    # ── Health & Monitoring ──────────────────────────────

    def health_check(self) -> bool:
        """Check if the database is reachable."""
        try:
            result = self.fetch_one("SELECT 1 AS health")
            return result is not None
        except Exception:
            return False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def pool_stats(self) -> dict[str, Any]:
        """Get connection pool statistics."""
        if not self._pool:
            return {"status": "not_connected"}

        return {
            "status": "connected",
            "min_connections": self._min_conn,
            "max_connections": self._max_conn,
        }

    def _ensure_connected(self) -> None:
        """Check that the pool is initialized.

        Raises:
            DatabaseError: If not connected.
        """
        if not self._pool or not self._is_connected:
            raise DatabaseError(
                "Database not connected. Call connect() first.",
                details={"is_connected": self._is_connected},
            )

    def __del__(self):
        try:
            self.disconnect()
        except Exception:
            pass


# ── Schema Migrations (Idempotent) ───────────────────────────

SCHEMA_MIGRATIONS = [
    # ── Extension for UUID support ──
    "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";",

    # ── Sectors ──
    """
    CREATE TABLE IF NOT EXISTS sectors (
        id              SERIAL PRIMARY KEY,
        name            VARCHAR(50) UNIQUE NOT NULL,
        display_name    VARCHAR(100) NOT NULL,
        nifty_index     VARCHAR(50) DEFAULT '',
        is_active       BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # ── Stocks (Master Instrument List) ──
    """
    CREATE TABLE IF NOT EXISTS stocks (
        id              SERIAL PRIMARY KEY,
        symbol          VARCHAR(30) NOT NULL,
        token           VARCHAR(20) NOT NULL,
        name            VARCHAR(200) DEFAULT '',
        exchange        VARCHAR(10) NOT NULL DEFAULT 'NSE',
        series          VARCHAR(10) DEFAULT 'EQ',
        sector_id       INTEGER REFERENCES sectors(id),
        industry        VARCHAR(200) DEFAULT '',
        market_cap_cr   NUMERIC(14,2) DEFAULT 0,
        lot_size        INTEGER DEFAULT 1,
        is_active       BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(symbol, exchange)
    );
    CREATE INDEX IF NOT EXISTS idx_stocks_symbol ON stocks(symbol);
    CREATE INDEX IF NOT EXISTS idx_stocks_token ON stocks(token);
    CREATE INDEX IF NOT EXISTS idx_stocks_sector ON stocks(sector_id);
    """,

    # ── Candle Tables (one per interval) ──
    # Each interval gets its own table for optimal query performance.
    # Table naming: candles_1m, candles_5m, candles_15m, candles_1h, candles_1d, candles_1w
    # The daily table (candles_1d) has extra delivery columns from NSE bhavcopy.
    """
    CREATE TABLE IF NOT EXISTS candles_1m (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        open            NUMERIC(12,2) NOT NULL,
        high            NUMERIC(12,2) NOT NULL,
        low             NUMERIC(12,2) NOT NULL,
        close           NUMERIC(12,2) NOT NULL,
        volume          BIGINT NOT NULL DEFAULT 0,
        vwap            NUMERIC(12,2) DEFAULT 0,
        UNIQUE(stock_id, timestamp)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_1m_stock_ts
        ON candles_1m(stock_id, timestamp DESC);
    """,

    """
    CREATE TABLE IF NOT EXISTS candles_5m (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        open            NUMERIC(12,2) NOT NULL,
        high            NUMERIC(12,2) NOT NULL,
        low             NUMERIC(12,2) NOT NULL,
        close           NUMERIC(12,2) NOT NULL,
        volume          BIGINT NOT NULL DEFAULT 0,
        vwap            NUMERIC(12,2) DEFAULT 0,
        UNIQUE(stock_id, timestamp)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_5m_stock_ts
        ON candles_5m(stock_id, timestamp DESC);
    """,

    """
    CREATE TABLE IF NOT EXISTS candles_15m (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        open            NUMERIC(12,2) NOT NULL,
        high            NUMERIC(12,2) NOT NULL,
        low             NUMERIC(12,2) NOT NULL,
        close           NUMERIC(12,2) NOT NULL,
        volume          BIGINT NOT NULL DEFAULT 0,
        vwap            NUMERIC(12,2) DEFAULT 0,
        UNIQUE(stock_id, timestamp)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_15m_stock_ts
        ON candles_15m(stock_id, timestamp DESC);
    """,

    """
    CREATE TABLE IF NOT EXISTS candles_1h (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        open            NUMERIC(12,2) NOT NULL,
        high            NUMERIC(12,2) NOT NULL,
        low             NUMERIC(12,2) NOT NULL,
        close           NUMERIC(12,2) NOT NULL,
        volume          BIGINT NOT NULL DEFAULT 0,
        vwap            NUMERIC(12,2) DEFAULT 0,
        UNIQUE(stock_id, timestamp)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_1h_stock_ts
        ON candles_1h(stock_id, timestamp DESC);
    """,

    """
    CREATE TABLE IF NOT EXISTS candles_1d (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        open            NUMERIC(12,2) NOT NULL,
        high            NUMERIC(12,2) NOT NULL,
        low             NUMERIC(12,2) NOT NULL,
        close           NUMERIC(12,2) NOT NULL,
        volume          BIGINT NOT NULL DEFAULT 0,
        vwap            NUMERIC(12,2) DEFAULT 0,
        delivery_qty    BIGINT DEFAULT 0,
        delivery_pct    NUMERIC(6,2) DEFAULT 0,
        UNIQUE(stock_id, timestamp)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_1d_stock_ts
        ON candles_1d(stock_id, timestamp DESC);
    """,

    # Weekly candles — aggregated from daily, kept for lifetime
    """
    CREATE TABLE IF NOT EXISTS candles_1w (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        open            NUMERIC(12,2) NOT NULL,
        high            NUMERIC(12,2) NOT NULL,
        low             NUMERIC(12,2) NOT NULL,
        close           NUMERIC(12,2) NOT NULL,
        volume          BIGINT NOT NULL DEFAULT 0,
        vwap            NUMERIC(12,2) DEFAULT 0,
        UNIQUE(stock_id, timestamp)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_1w_stock_ts
        ON candles_1w(stock_id, timestamp DESC);
    """,

    # ── Candle Coverage (data range tracker per stock per interval) ──
    # Tracks what data we have so we know what's missing and can fetch gaps.
    """
    CREATE TABLE IF NOT EXISTS candle_coverage (
        id              SERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        interval        VARCHAR(20) NOT NULL,
        earliest_ts     TIMESTAMPTZ NOT NULL,
        latest_ts       TIMESTAMPTZ NOT NULL,
        candle_count    INTEGER DEFAULT 0,
        source          VARCHAR(30) DEFAULT 'angel_one',
        last_updated    TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(stock_id, interval)
    );
    CREATE INDEX IF NOT EXISTS idx_candle_coverage_stock
        ON candle_coverage(stock_id);
    CREATE INDEX IF NOT EXISTS idx_candle_coverage_interval
        ON candle_coverage(interval);
    """,

    # ── Watchlists (parent: list metadata) ──
    # Each watchlist is a named collection of stocks.
    # System creates default lists like 'live_subscribe' (WebSocket),
    # 'top_volume' (auto-populated), etc.
    """
    CREATE TABLE IF NOT EXISTS watchlists (
        id              SERIAL PRIMARY KEY,
        name            VARCHAR(100) UNIQUE NOT NULL,
        description     TEXT DEFAULT '',
        list_type       VARCHAR(30) DEFAULT 'custom',
        is_active       BOOLEAN DEFAULT TRUE,
        max_stocks      INTEGER DEFAULT 500,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # ── Watchlist Items (child: stocks in a watchlist) ──
    """
    CREATE TABLE IF NOT EXISTS watchlist_items (
        id              SERIAL PRIMARY KEY,
        watchlist_id    INTEGER REFERENCES watchlists(id) ON DELETE CASCADE,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        source          VARCHAR(30) DEFAULT 'manual',
        priority        INTEGER DEFAULT 0,
        added_at        TIMESTAMPTZ DEFAULT NOW(),
        notes           TEXT DEFAULT '',
        UNIQUE(watchlist_id, stock_id)
    );
    CREATE INDEX IF NOT EXISTS idx_watchlist_items_watchlist
        ON watchlist_items(watchlist_id);
    CREATE INDEX IF NOT EXISTS idx_watchlist_items_stock
        ON watchlist_items(stock_id);
    """,

    # Seed default watchlists
    """
    INSERT INTO watchlists (name, description, list_type, max_stocks)
    VALUES
        ('nse500', 'NSE 500 index stocks — default for WebSocket and scanning', 'system', 500),
        ('manual_picks', 'Manually curated stocks to watch', 'custom', 0)
    ON CONFLICT (name) DO NOTHING;
    """,

    # ── Volume Snapshots (intraday cumulative) ──
    """
    CREATE TABLE IF NOT EXISTS volume_snapshots (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        timestamp       TIMESTAMPTZ NOT NULL,
        cumulative_vol  BIGINT NOT NULL,
        avg_vol_30d     BIGINT DEFAULT 0,
        avg_vol_90d     BIGINT DEFAULT 0,
        volume_ratio    NUMERIC(8,2) DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_volume_snapshots_stock_ts
        ON volume_snapshots(stock_id, timestamp DESC);
    """,

    # ── Broker Accounts ──
    """
    CREATE TABLE IF NOT EXISTS broker_accounts (
        id              SERIAL PRIMARY KEY,
        broker_name     VARCHAR(50) NOT NULL DEFAULT 'angel_one',
        client_id       VARCHAR(50) NOT NULL,
        is_active       BOOLEAN DEFAULT TRUE,
        is_paper        BOOLEAN DEFAULT FALSE,
        static_ip       VARCHAR(45) DEFAULT '',
        last_login_at   TIMESTAMPTZ,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(broker_name, client_id)
    );
    """,

    # ── Signals ──
    """
    CREATE TABLE IF NOT EXISTS signals (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id),
        sector_id       INTEGER REFERENCES sectors(id),
        signal_type     VARCHAR(10) NOT NULL,
        strength        VARCHAR(20) NOT NULL,
        score           NUMERIC(5,3) NOT NULL,
        analyzer_name   VARCHAR(50) DEFAULT '',
        analyzer_confidence NUMERIC(5,3) DEFAULT 0,
        analyzer_reasoning  TEXT DEFAULT '',
        volume_current  BIGINT DEFAULT 0,
        volume_avg      BIGINT DEFAULT 0,
        volume_ratio    NUMERIC(8,2) DEFAULT 0,
        price           NUMERIC(12,2) NOT NULL,
        status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
        metadata        JSONB DEFAULT '{}',
        created_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_signals_stock ON signals(stock_id);
    CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
    CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
    """,

    # ── Orders ──
    """
    CREATE TABLE IF NOT EXISTS orders (
        id              BIGSERIAL PRIMARY KEY,
        signal_id       BIGINT REFERENCES signals(id),
        stock_id        INTEGER REFERENCES stocks(id),
        order_type      VARCHAR(20) NOT NULL,
        product_type    VARCHAR(20) NOT NULL DEFAULT 'INTRADAY',
        transaction_type VARCHAR(10) NOT NULL,
        quantity         INTEGER NOT NULL,
        price           NUMERIC(12,2) NOT NULL,
        trigger_price   NUMERIC(12,2) DEFAULT 0,
        status          VARCHAR(20) NOT NULL DEFAULT 'CREATED',
        is_paper_trade  BOOLEAN DEFAULT TRUE,
        broker_order_id VARCHAR(50) DEFAULT '',
        filled_qty      INTEGER DEFAULT 0,
        avg_fill_price  NUMERIC(12,2) DEFAULT 0,
        slippage        NUMERIC(6,4) DEFAULT 0,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_orders_signal ON orders(signal_id);
    CREATE INDEX IF NOT EXISTS idx_orders_stock ON orders(stock_id);
    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
    """,

    # ── Positions (current holdings) ──
    """
    CREATE TABLE IF NOT EXISTS positions (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id),
        quantity        INTEGER NOT NULL,
        avg_buy_price   NUMERIC(12,2) NOT NULL,
        current_price   NUMERIC(12,2) DEFAULT 0,
        unrealized_pnl  NUMERIC(14,2) DEFAULT 0,
        is_paper        BOOLEAN DEFAULT TRUE,
        product_type    VARCHAR(20) NOT NULL DEFAULT 'INTRADAY',
        opened_at       TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(stock_id, is_paper, product_type)
    );
    CREATE INDEX IF NOT EXISTS idx_positions_stock ON positions(stock_id);
    """,

    # ── Daily P&L ──
    """
    CREATE TABLE IF NOT EXISTS daily_pnl (
        id              SERIAL PRIMARY KEY,
        date            DATE NOT NULL,
        realized_pnl    NUMERIC(14,2) DEFAULT 0,
        unrealized_pnl  NUMERIC(14,2) DEFAULT 0,
        total_trades    INTEGER DEFAULT 0,
        winning_trades  INTEGER DEFAULT 0,
        losing_trades   INTEGER DEFAULT 0,
        max_drawdown    NUMERIC(14,2) DEFAULT 0,
        portfolio_value NUMERIC(16,2) DEFAULT 0,
        cash_balance    NUMERIC(16,2) DEFAULT 0,
        is_paper        BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(date, is_paper)
    );
    """,

    # ── Audit Log (immutable) ──
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id              BIGSERIAL PRIMARY KEY,
        timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        event_type      VARCHAR(50) NOT NULL,
        details         JSONB DEFAULT '{}',
        ip_address      VARCHAR(45) DEFAULT '',
        checksum        VARCHAR(64) NOT NULL,
        prev_checksum   VARCHAR(64) DEFAULT '',
        created_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_audit_log_event ON audit_log(event_type);
    CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(timestamp DESC);
    """,

    # ── Live Ticks (raw real-time data, auto-purged) ──
    """
    CREATE TABLE IF NOT EXISTS live_ticks (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id),
        token           VARCHAR(20) NOT NULL,
        timestamp       TIMESTAMPTZ NOT NULL,
        ltp             NUMERIC(12,2) NOT NULL,
        volume          BIGINT DEFAULT 0,
        open            NUMERIC(12,2) DEFAULT 0,
        high            NUMERIC(12,2) DEFAULT 0,
        low             NUMERIC(12,2) DEFAULT 0,
        best_bid        NUMERIC(12,2) DEFAULT 0,
        best_ask        NUMERIC(12,2) DEFAULT 0,
        avg_trade_price NUMERIC(12,2) DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_live_ticks_token_ts
        ON live_ticks(token, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_live_ticks_ts
        ON live_ticks(timestamp DESC);
    """,



    # ── Technical Indicators (EOD cache) ──
    """
    CREATE TABLE IF NOT EXISTS technical_indicators (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        date            DATE NOT NULL,
        rsi_14          NUMERIC(6,2),
        ema_20          NUMERIC(12,2),
        ema_50          NUMERIC(12,2),
        atr_14          NUMERIC(12,2),
        macd_line       NUMERIC(12,2),
        macd_signal     NUMERIC(12,2),
        delivery_pct    NUMERIC(6,2),
        UNIQUE(stock_id, date)
    );
    CREATE INDEX IF NOT EXISTS idx_tech_ind_stock_date
        ON technical_indicators(stock_id, date DESC);
    """,

    # ── Technical Indicators — Column Expansion ──
    # Adds all indicator columns missing from the original CREATE TABLE.
    # Uses ADD COLUMN IF NOT EXISTS for idempotent re-runs.
    """
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS sma_20 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS sma_50 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS sma_200 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS macd_histogram NUMERIC(12,4);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS bollinger_upper NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS bollinger_lower NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS bollinger_width NUMERIC(8,4);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS supertrend NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS supertrend_direction INTEGER DEFAULT 0;
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS adx_14 NUMERIC(6,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS obv BIGINT;
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS pivot_point NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS pivot_r1 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS pivot_r2 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS pivot_s1 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS pivot_s2 NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS support_level NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS resistance_level NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS high_52w NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS low_52w NUMERIC(12,2);
    ALTER TABLE technical_indicators ADD COLUMN IF NOT EXISTS vwap_daily NUMERIC(12,2);
    """,

    # ── Sector History (Breadth/Momentum) ──
    """
    CREATE TABLE IF NOT EXISTS sector_history (
        id                SERIAL PRIMARY KEY,
        sector_id         INTEGER REFERENCES sectors(id) ON DELETE CASCADE,
        date              DATE NOT NULL,
        advancing_stocks  INTEGER DEFAULT 0,
        declining_stocks  INTEGER DEFAULT 0,
        total_volume      BIGINT DEFAULT 0,
        heat_score        NUMERIC(5,2) DEFAULT 0.5,
        UNIQUE(sector_id, date)
    );
    """,

    # ── Scan History (Scanner stats for backtesting) ──
    """
    CREATE TABLE IF NOT EXISTS scan_history (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        date            DATE NOT NULL,
        peak_vol_ratio  NUMERIC(8,2) DEFAULT 0,
        signals_fired   INTEGER DEFAULT 0,
        UNIQUE(stock_id, date)
    );
    """,

    # ── Scanner Signals (post-market → next-day pipeline) ──
    # Persists overnight batch scanner results so algorithms can pre-seed
    # their watchlists at morning warm-up.
    """
    CREATE TABLE IF NOT EXISTS scanner_signals (
        id              BIGSERIAL PRIMARY KEY,
        stock_id        INTEGER REFERENCES stocks(id) ON DELETE CASCADE,
        scan_date       DATE NOT NULL,
        target_date     DATE NOT NULL,
        scanner_name    VARCHAR(50) NOT NULL,
        pattern         VARCHAR(50) NOT NULL,
        direction       VARCHAR(10) NOT NULL,
        confidence      NUMERIC(5,3) NOT NULL,
        price           NUMERIC(12,2) NOT NULL,
        strength        VARCHAR(20) NOT NULL,
        details         JSONB DEFAULT '{}',
        status          VARCHAR(20) DEFAULT 'ACTIVE',
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(stock_id, scan_date, scanner_name, pattern)
    );
    CREATE INDEX IF NOT EXISTS idx_scanner_signals_target
        ON scanner_signals(target_date, status);
    CREATE INDEX IF NOT EXISTS idx_scanner_signals_stock
        ON scanner_signals(stock_id, target_date);
    """,

    # ── App Config (runtime-tunable parameters) ──
    """
    CREATE TABLE IF NOT EXISTS app_config (
        id              SERIAL PRIMARY KEY,
        key             VARCHAR(100) UNIQUE NOT NULL,
        value           TEXT NOT NULL,
        data_type       VARCHAR(20) NOT NULL DEFAULT 'string',
        category        VARCHAR(50) DEFAULT 'general',
        description     TEXT DEFAULT '',
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_by      VARCHAR(50) DEFAULT 'system'
    );
    """,

    # ── Seed default app_config values ──
    """
    INSERT INTO app_config (key, value, data_type, category, description)
    VALUES
        ('analyzer_type', 'static', 'string', 'analyzer', 'Active analyzer: static, gemini, composite'),
        ('spike_multiplier', '2.0', 'float', 'scanner', 'Volume spike threshold multiplier'),
        ('min_signal_score', '0.35', 'float', 'scanner', 'Minimum signal score to generate'),
        ('max_capital_per_trade_pct', '5.0', 'float', 'risk', 'Max capital % per trade'),
        ('max_daily_loss_pct', '2.0', 'float', 'risk', 'Max daily loss % before halt'),
        ('max_open_positions', '20', 'int', 'risk', 'Max simultaneous open positions'),
        ('default_stop_loss_pct', '2.0', 'float', 'risk', 'Default stop loss percentage'),
        ('default_target_pct', '5.0', 'float', 'risk', 'Default target percentage'),
        ('trading_mode', 'PAPER', 'string', 'trading', 'Trading mode: PAPER, LIVE, DUAL'),
        ('min_price', '1.0', 'float', 'filter', 'Minimum stock price for scanning'),
        ('max_price', '99999.0', 'float', 'filter', 'Maximum stock price for scanning'),
        ('min_avg_volume', '10000', 'int', 'filter', 'Minimum 30-day avg volume'),
        ('slippage_pct', '0.1', 'float', 'trading', 'Simulated slippage percentage'),
        ('circuit_breaker_daily_loss_pct', '3.0', 'float', 'circuit_breaker', 'Circuit breaker daily loss trigger'),
        ('circuit_breaker_consecutive_losses', '5', 'int', 'circuit_breaker', 'Max consecutive losses before halt')
    ON CONFLICT (key) DO NOTHING;
    """,

    # ── Algorithms Master Table ──
    # Registry of all available trading algorithms with metadata.
    """
    CREATE TABLE IF NOT EXISTS algorithms (
        id              SERIAL PRIMARY KEY,
        name            VARCHAR(100) UNIQUE NOT NULL,
        display_name    VARCHAR(200) NOT NULL,
        description     TEXT,
        strategy_type   VARCHAR(50) NOT NULL DEFAULT 'swing',
        candle_intervals VARCHAR(200),
        max_positions   INTEGER DEFAULT 5,
        stop_loss_pct   NUMERIC(5,2) DEFAULT 5.00,
        reference_url   TEXT,
        is_active       BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Seed the 3 existing algorithms
    """
    INSERT INTO algorithms (name, display_name, description, strategy_type, candle_intervals, max_positions, stop_loss_pct)
    VALUES
        ('connors_rsi_pullback', 'Connors RSI Pullback',
         'Mean-reversion: buys oversold stocks (RSI<25) in uptrends (price>SMA200). Exits on RSI>40 or 10-day time stop.',
         'swing', 'ONE_DAY', 5, 5.00),
        ('rayner_dip_buyer', 'Rayner 10-Day Low Dip Buyer',
         'Buys 10-day lows at 2%% discount in uptrends (price>SMA200). Exits when close>SMA5 or 20-day time stop.',
         'positional', 'ONE_DAY', 10, 5.00),
        ('scanner_confluence', 'Scanner Confluence',
         'Meta-algorithm: enters when 2+ scanners agree on same stock. Intraday with 3%% SL / 2%% TP.',
         'intraday', 'FIVE_MINUTE', 5, 3.00)
    ON CONFLICT (name) DO NOTHING;
    """,

    # ── Watchlist ↔ Algorithm Bindings (routing table) ──
    # Maps which algorithms are allowed to trade stocks from which watchlists.
    # If no bindings exist for an algorithm, it falls back to receiving all observations.
    """
    CREATE TABLE IF NOT EXISTS watchlist_algorithm_bindings (
        id              SERIAL PRIMARY KEY,
        watchlist_id    INTEGER REFERENCES watchlists(id) ON DELETE CASCADE,
        algorithm_name  VARCHAR(100) NOT NULL REFERENCES algorithms(name),
        is_active       BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(watchlist_id, algorithm_name)
    );
    CREATE INDEX IF NOT EXISTS idx_wab_watchlist
        ON watchlist_algorithm_bindings(watchlist_id);
    CREATE INDEX IF NOT EXISTS idx_wab_algorithm
        ON watchlist_algorithm_bindings(algorithm_name);
    """,

    # ── Seed additional watchlists for multi-strategy routing ──
    """
    INSERT INTO watchlists (name, description, list_type, max_stocks)
    VALUES
        ('hot_stocks', 'High momentum stocks with recent volume surges', 'custom', 100),
        ('trending_breakouts', 'Stocks breaking out of consolidation patterns', 'custom', 50),
        ('quality_value', 'Blue-chip stocks with strong fundamentals for pullback buying', 'custom', 50)
    ON CONFLICT (name) DO NOTHING;
    """,

    # ── Backtest Runs (each run = a "conversation" in backtest history) ──
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id                  SERIAL PRIMARY KEY,
        algorithm_name      VARCHAR(100) NOT NULL,
        stock_symbol        VARCHAR(30) NOT NULL,
        stock_name          VARCHAR(200) DEFAULT '',
        start_date          DATE NOT NULL,
        end_date            DATE NOT NULL,
        initial_capital     NUMERIC(16,2) DEFAULT 1000000,
        status              VARCHAR(20) DEFAULT 'running',
        total_return_pct    NUMERIC(10,2),
        cagr_pct            NUMERIC(10,2),
        max_drawdown_pct    NUMERIC(10,2),
        sharpe_ratio        NUMERIC(8,3),
        win_rate_pct        NUMERIC(6,2),
        total_trades        INTEGER,
        winning_trades      INTEGER DEFAULT 0,
        losing_trades       INTEGER DEFAULT 0,
        avg_win_pct         NUMERIC(8,2),
        avg_loss_pct        NUMERIC(8,2),
        profit_factor       NUMERIC(8,3),
        avg_hold_days       NUMERIC(6,1),
        final_capital       NUMERIC(16,2),
        equity_curve        JSONB DEFAULT '[]',
        config              JSONB DEFAULT '{}',
        error_message       TEXT DEFAULT '',
        created_at          TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_bt_runs_created ON backtest_runs(created_at DESC);
    """,

    # ── Backtest Trades (individual trades within a run) ──
    """
    CREATE TABLE IF NOT EXISTS backtest_trades (
        id              SERIAL PRIMARY KEY,
        run_id          INTEGER REFERENCES backtest_runs(id) ON DELETE CASCADE,
        symbol          VARCHAR(30) NOT NULL,
        entry_date      DATE NOT NULL,
        exit_date       DATE,
        entry_price     NUMERIC(12,2) NOT NULL,
        exit_price      NUMERIC(12,2),
        quantity        INTEGER DEFAULT 0,
        pnl             NUMERIC(14,2) DEFAULT 0,
        pnl_pct         NUMERIC(8,2) DEFAULT 0,
        exit_reason     VARCHAR(50),
        hold_days       INTEGER DEFAULT 0,
        metadata        JSONB DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);
    """,
]
