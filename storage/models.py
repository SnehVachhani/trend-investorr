"""
Trend-Investorr: Storage Models (Repository Layer)

CRUD operations for all database tables. Each model class
wraps SQL queries for a specific table and handles
serialization/deserialization to/from dataclasses.

Architecture:
    Dataclass (market_data.py) ↔ StorageModel (models.py) ↔ Database (database.py)

All queries use parameterized statements (no SQL injection).
All writes go through the Database transaction context manager.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, date, timedelta, time as dt_time
from typing import Any

from loguru import logger
from psycopg2.extras import Json

from core.constants import IST, MARKET_CLOSE, MARKET_OPEN
from core.universe import equity_universe_sql
from data.candle_sessions import (
    INTRADAY_TIMEFRAMES,
    is_regular_session_candle,
    normalize_daily_candles,
)
from storage.database import Database


def _canonical_daily_timestamp(timestamp: datetime | date) -> datetime:
    """Key every new daily candle by its NSE session close, not provider time."""
    if isinstance(timestamp, datetime):
        local = timestamp.replace(tzinfo=IST) if timestamp.tzinfo is None else timestamp.astimezone(IST)
        session_date = local.date()
    else:
        session_date = timestamp
    return datetime.combine(session_date, MARKET_CLOSE, tzinfo=IST)


class AppConfigStore:
    """
    Runtime configuration CRUD.

    Reads/writes the app_config table for runtime-tunable parameters.
    Caches values in-memory with a configurable TTL.
    """

    def __init__(self, db: Database):
        self._db = db
        self._cache: dict[str, str] = {}
        self._cache_loaded = False

    def get(self, key: str, default: str = "") -> str:
        """Get a config value by key."""
        self._ensure_cache()
        return self._cache.get(key, default)

    def get_float(self, key: str, default: float = 0.0) -> float:
        """Get a config value as float."""
        try:
            return float(self.get(key, str(default)))
        except ValueError:
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        """Get a config value as int."""
        try:
            return int(float(self.get(key, str(default))))
        except ValueError:
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a config value as bool."""
        val = self.get(key, str(default)).lower()
        return val in ("true", "1", "yes", "on")

    def set(self, key: str, value: str, category: str = "general") -> None:
        """Set a config value (upsert)."""
        try:
            self._db.execute(
                """
                INSERT INTO app_config (key, value, category, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
                """,
                (key, value, category),
            )
            self._cache[key] = value
            logger.debug(f"Config set: {key}={value}")
        except Exception as e:
            logger.error(f"Failed to set config {key}: {e}")

    def get_all(self, category: str | None = None) -> list[dict]:
        """Get all config values, optionally filtered by category."""
        if category:
            return self._db.fetch_all(
                "SELECT key, value, data_type, category, description FROM app_config WHERE category = %s ORDER BY key",
                (category,),
            )
        return self._db.fetch_all(
            "SELECT key, value, data_type, category, description FROM app_config ORDER BY category, key"
        )

    def reload(self) -> int:
        """Reload cache from database. Returns count of loaded values."""
        self._cache.clear()
        self._cache_loaded = False
        self._ensure_cache()
        return len(self._cache)

    def _ensure_cache(self) -> None:
        """Load all config values into memory cache."""
        if self._cache_loaded:
            return
        try:
            rows = self._db.fetch_all("SELECT key, value FROM app_config")
            self._cache = {row["key"]: row["value"] for row in rows}
            self._cache_loaded = True
            logger.debug(f"AppConfig cache loaded: {len(self._cache)} entries")
        except Exception as e:
            logger.warning(f"Failed to load app_config cache: {e}")
            self._cache_loaded = True  # Don't retry on every access


class StockStore:
    """CRUD operations for the stocks table."""

    def __init__(self, db: Database):
        self._db = db

    def upsert(self, symbol: str, token: str, name: str = "",
               exchange: str = "NSE", series: str = "EQ",
               industry: str = "", lot_size: int = 1) -> int | None:
        """Insert or update a stock. Returns the stock ID."""
        row = self._db.execute_returning(
            """
            INSERT INTO stocks (symbol, token, name, exchange, series, industry, lot_size, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol, exchange) DO UPDATE SET
                token = EXCLUDED.token,
                name = EXCLUDED.name,
                series = EXCLUDED.series,
                industry = EXCLUDED.industry,
                lot_size = EXCLUDED.lot_size,
                is_active = TRUE,
                updated_at = NOW()
            RETURNING id
            """,
            (symbol, token, name, exchange, series, industry, lot_size),
        )
        return row["id"] if row else None

    def upsert_batch(self, stocks: list[tuple]) -> int:
        """
        Batch upsert stocks.

        Args:
            stocks: List of (symbol, token, name, exchange, series, industry, lot_size).
        """
        return self._db.execute_many(
            """
            INSERT INTO stocks (symbol, token, name, exchange, series, industry, lot_size, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol, exchange) DO UPDATE SET
                token = EXCLUDED.token, name = EXCLUDED.name,
                series = EXCLUDED.series, industry = EXCLUDED.industry,
                lot_size = EXCLUDED.lot_size, is_active = TRUE,
                updated_at = NOW()
            """,
            stocks,
        )

    def get_by_symbol(self, symbol: str, exchange: str = "NSE") -> dict | None:
        return self._db.fetch_one(
            "SELECT * FROM stocks WHERE symbol = %s AND exchange = %s",
            (symbol, exchange),
        )

    def get_by_token(self, token: str) -> dict | None:
        return self._db.fetch_one(
            "SELECT * FROM stocks WHERE token = %s", (token,),
        )

    def get_ids_by_tokens(self, tokens: list[str], exchange: str = "NSE") -> dict[str, int]:
        """
        Resolve SmartAPI tokens to stock IDs in a single query.

        SmartAPI token numbers overlap between exchanges (an NSE token can
        equal an unrelated BSE token), so only active rows of the requested
        exchange are considered — the live feed subscribes NSE tokens.

        Returns:
            {token: stock_id} for every token with an active row on
            ``exchange``. Tokens without one are simply absent.
        """
        tokens = [str(t) for t in tokens if t]
        if not tokens:
            return {}
        rows = self._db.fetch_all(
            """
            SELECT id, token FROM stocks
            WHERE token = ANY(%s) AND exchange = %s AND is_active = TRUE
            ORDER BY id
            """,
            (tokens, exchange),
        )
        mapping: dict[str, int] = {}
        for row in rows:
            mapping.setdefault(str(row["token"]), int(row["id"]))
        return mapping

    def get_by_sector(self, sector_id: int) -> list[dict]:
        return self._db.fetch_all(
            f"SELECT * FROM stocks WHERE sector_id = %s AND {equity_universe_sql('stocks')} ORDER BY symbol",
            (sector_id,),
        )

    def get_all_active(self, exchange: str = "NSE") -> list[dict]:
        return self._db.fetch_all(
            f"SELECT * FROM stocks WHERE {equity_universe_sql('stocks')} AND stocks.exchange = %s ORDER BY symbol",
            (exchange,),
        )

    def update_sector(self, stock_id: int, sector_id: int) -> None:
        self._db.execute(
            "UPDATE stocks SET sector_id = %s, updated_at = NOW() WHERE id = %s",
            (sector_id, stock_id),
        )

    def update_upstox_identity(self, stock_id: int, isin: str, instrument_key: str) -> None:
        """Persist the stable ISIN bridge used by fundamentals providers."""
        self._db.execute(
            """
            UPDATE stocks
            SET isin = %s,
                upstox_instrument_key = %s,
                isin_updated_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            """,
            (isin, instrument_key, stock_id),
        )

    def get_fundamental_universe(self, limit: int = 0) -> list[dict]:
        """Return active equity stocks and their provider identity fields."""
        query = f"""
            SELECT id, symbol, name, exchange, series, isin, upstox_instrument_key
            FROM stocks
            WHERE {equity_universe_sql('stocks')}
            ORDER BY symbol
        """
        if limit > 0:
            query += " LIMIT %s"
            return self._db.fetch_all(query, (limit,))
        return self._db.fetch_all(query)


class WatchlistStore:
    """
    CRUD for watchlists and watchlist_items.

    Supports multiple named watchlists (nse500, manual_picks, etc.)
    with deduplication across all lists for WebSocket subscriptions.
    """

    def __init__(self, db):
        self._db = db

    # ── Watchlist (parent) ──

    def create_watchlist(
        self, name: str, description: str = "",
        list_type: str = "custom", max_stocks: int = 500,
    ) -> int | None:
        """Create a new watchlist. Returns the watchlist ID."""
        row = self._db.execute_returning(
            """
            INSERT INTO watchlists (name, description, list_type, max_stocks)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                list_type = EXCLUDED.list_type,
                max_stocks = EXCLUDED.max_stocks,
                is_active = TRUE,
                updated_at = NOW()
            RETURNING id
            """,
            (name, description, list_type, max_stocks),
        )
        return row["id"] if row else None

    def delete_watchlist(self, name: str) -> bool:
        """Soft-delete a watchlist by setting is_active = FALSE."""
        affected = self._db.execute(
            "UPDATE watchlists SET is_active = FALSE, updated_at = NOW() WHERE name = %s",
            (name,),
        )
        return affected > 0

    def get_watchlist(self, name: str) -> dict | None:
        """Get a watchlist by name."""
        return self._db.fetch_one(
            "SELECT * FROM watchlists WHERE name = %s", (name,)
        )

    def get_all_active(self) -> list[dict]:
        """Get all active watchlists."""
        return self._db.fetch_all(
            "SELECT * FROM watchlists WHERE is_active = TRUE ORDER BY name"
        )

    def get_all_with_counts(self) -> list[dict]:
        """
        Get all active watchlists with stock counts and bound algorithm names.

        Returns list of dicts:
            {id, name, description, list_type, stock_count, bound_algorithms}
        """
        return self._db.fetch_all(
            f"""
            SELECT
                wl.id, wl.name, wl.description, wl.list_type,
                wl.max_stocks, wl.is_active,
                COUNT(DISTINCT wi.stock_id) FILTER (WHERE {equity_universe_sql('s')}) AS stock_count,
                COALESCE(
                    ARRAY_AGG(DISTINCT wab.algorithm_name)
                    FILTER (WHERE wab.algorithm_name IS NOT NULL AND wab.is_active = TRUE),
                    ARRAY[]::VARCHAR[]
                ) AS bound_algorithms
            FROM watchlists wl
            LEFT JOIN watchlist_items wi ON wi.watchlist_id = wl.id
            LEFT JOIN stocks s ON s.id = wi.stock_id
            LEFT JOIN watchlist_algorithm_bindings wab ON wab.watchlist_id = wl.id
            WHERE wl.is_active = TRUE
            GROUP BY wl.id
            ORDER BY wl.name
            """
        )

    # ── Watchlist Items (child) ──

    def add_stock(self, watchlist_name: str, stock_id: int, source: str = "manual", notes: str = "") -> bool:
        """Add a stock to the top of a watchlist.

        Priority is displayed in descending order.  The old insert relied on
        the database default (zero), which silently placed every new stock
        after the manually ordered entries.  Locking the parent watchlist
        serializes additions to the same list, so two quick adds cannot claim
        the same next priority.
        """
        wl = self.get_watchlist(watchlist_name)
        if not wl:
            return False

        with self._db.transaction() as cur:
            # Serialize insertion order within one watchlist while retaining
            # independent concurrency for other watchlists.
            cur.execute(
                "SELECT id, max_stocks FROM watchlists WHERE id = %s FOR UPDATE",
                (wl["id"],),
            )
            locked_watchlist = cur.fetchone()
            if not locked_watchlist:
                return False

            # Check the capacity after acquiring the lock, otherwise two
            # parallel requests could both pass the old count check.
            if locked_watchlist["max_stocks"] > 0:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM watchlist_items WHERE watchlist_id = %s",
                    (locked_watchlist["id"],),
                )
                count = cur.fetchone()
                if count and count["cnt"] >= locked_watchlist["max_stocks"]:
                    return False

            cur.execute(
                """
                INSERT INTO watchlist_items (watchlist_id, stock_id, source, notes, priority)
                SELECT %s, %s, %s, %s, COALESCE(MAX(priority), 0) + 1
                FROM watchlist_items
                WHERE watchlist_id = %s
                ON CONFLICT (watchlist_id, stock_id) DO NOTHING
                """,
                (locked_watchlist["id"], stock_id, source, notes, locked_watchlist["id"]),
            )
            return cur.rowcount > 0

    def remove_stock(self, watchlist_name: str, stock_id: int) -> None:
        """Remove a stock from a watchlist."""
        wl = self.get_watchlist(watchlist_name)
        if wl:
            self._db.execute(
                "DELETE FROM watchlist_items WHERE watchlist_id = %s AND stock_id = %s",
                (wl["id"], stock_id),
            )

    def get_items(self, watchlist_name: str) -> list[dict]:
        """Get all stocks in a watchlist with stock details."""
        return self._db.fetch_all(
            f"""
            SELECT wi.*, s.symbol, s.token, s.name, s.exchange
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            JOIN watchlists wl ON wl.id = wi.watchlist_id
            WHERE wl.name = %s AND {equity_universe_sql('s')}
            ORDER BY wi.priority DESC, s.symbol
            """,
            (watchlist_name,),
        )

    def get_subscribe_tokens(self) -> list[str]:
        """
        Get UNIQUE stock tokens from ALL active watchlists.

        Deduplicates across watchlists — if RELIANCE is in both
        nse500 and manual_picks, it appears only once.

        Returns:
            List of unique SmartAPI tokens for WebSocket subscription.
        """
        rows = self._db.fetch_all(
            f"""
            SELECT DISTINCT s.token
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            JOIN watchlists wl ON wl.id = wi.watchlist_id
            WHERE wl.is_active = TRUE AND {equity_universe_sql('s')}
            ORDER BY s.token
            """
        )
        return [r["token"] for r in rows]

    def populate_from_volume(self, watchlist_name: str, limit: int = 500) -> int:
        """
        Auto-populate a watchlist with top stocks by avg daily volume.

        Reads from candles_1d to calculate 30-day avg volume,
        then inserts the top N stocks.

        Returns number of stocks added.
        """
        wl = self.get_watchlist(watchlist_name)
        if not wl:
            return 0

        # Clear existing auto-populated entries
        self._db.execute(
            "DELETE FROM watchlist_items WHERE watchlist_id = %s AND source = 'auto_volume'",
            (wl["id"],),
        )

        # Get top stocks by avg volume from candles_1d (last 30 days)
        top_stocks = self._db.fetch_all(
            f"""
            SELECT c.stock_id, AVG(c.volume) as avg_vol
            FROM candles_1d c
            JOIN stocks s ON s.id = c.stock_id
            WHERE c.timestamp > NOW() - INTERVAL '30 days'
              AND {equity_universe_sql('s')}
            GROUP BY c.stock_id
            HAVING AVG(c.volume) > 0
            ORDER BY avg_vol DESC
            LIMIT %s
            """,
            (limit,),
        )

        for row in top_stocks:
            self._db.execute(
                """
                INSERT INTO watchlist_items (watchlist_id, stock_id, source, priority)
                VALUES (%s, %s, 'auto_volume', %s)
                ON CONFLICT (watchlist_id, stock_id) DO UPDATE SET
                    priority = EXCLUDED.priority,
                    source = EXCLUDED.source
                """,
                (wl["id"], row["stock_id"], int(row["avg_vol"])),
            )

        return len(top_stocks)


class WatchlistAlgorithmBindingStore:
    """
    CRUD for watchlist ↔ algorithm bindings.

    Determines which algorithms are allowed to trade stocks
    from which watchlists. The routing table is built from these
    bindings and cached in AlgorithmManager at warm-up.

    Lifecycle:
        CLI/Admin → bind/unbind          [any time]
        scanner_warmup() → build_routing_table()  [9:00 AM]
        AlgorithmManager → uses routing for filtering  [live]
    """

    def __init__(self, db):
        self._db = db

    def bind(self, watchlist_name: str, algorithm_name: str) -> bool:
        """
        Bind an algorithm to a watchlist.

        Returns True if binding was created, False if watchlist not found.
        """
        wl = self._db.fetch_one(
            "SELECT id FROM watchlists WHERE name = %s AND is_active = TRUE",
            (watchlist_name,),
        )
        if not wl:
            return False

        self._db.execute(
            """
            INSERT INTO watchlist_algorithm_bindings (watchlist_id, algorithm_name)
            VALUES (%s, %s)
            ON CONFLICT (watchlist_id, algorithm_name) DO UPDATE SET
                is_active = TRUE,
                updated_at = NOW()
            """,
            (wl["id"], algorithm_name),
        )
        logger.info(f"Bound algorithm '{algorithm_name}' to watchlist '{watchlist_name}'")
        return True

    def unbind(self, watchlist_name: str, algorithm_name: str) -> bool:
        """
        Unbind an algorithm from a watchlist (soft-delete).

        Returns True if a binding was deactivated.
        """
        wl = self._db.fetch_one(
            "SELECT id FROM watchlists WHERE name = %s",
            (watchlist_name,),
        )
        if not wl:
            return False

        affected = self._db.execute(
            """
            UPDATE watchlist_algorithm_bindings
            SET is_active = FALSE, updated_at = NOW()
            WHERE watchlist_id = %s AND algorithm_name = %s
            """,
            (wl["id"], algorithm_name),
        )
        if affected:
            logger.info(f"Unbound algorithm '{algorithm_name}' from watchlist '{watchlist_name}'")
        return affected > 0

    def get_bindings_for_watchlist(self, watchlist_name: str) -> list[dict]:
        """Get all active algorithms bound to a watchlist."""
        return self._db.fetch_all(
            """
            SELECT wab.algorithm_name, wab.is_active, wab.created_at
            FROM watchlist_algorithm_bindings wab
            JOIN watchlists wl ON wl.id = wab.watchlist_id
            WHERE wl.name = %s AND wab.is_active = TRUE
            ORDER BY wab.algorithm_name
            """,
            (watchlist_name,),
        )

    def get_bindings_for_algorithm(self, algorithm_name: str) -> list[dict]:
        """Get all active watchlists bound to an algorithm."""
        return self._db.fetch_all(
            """
            SELECT wl.name, wl.description, wab.is_active, wab.created_at
            FROM watchlist_algorithm_bindings wab
            JOIN watchlists wl ON wl.id = wab.watchlist_id
            WHERE wab.algorithm_name = %s AND wab.is_active = TRUE
              AND wl.is_active = TRUE
            ORDER BY wl.name
            """,
            (algorithm_name,),
        )

    def build_routing_table(self) -> dict[str, set[str]]:
        """
        Build the NSE token → set[algorithm_name] routing table.

        Joins watchlist_algorithm_bindings → watchlist_items → stocks
        to produce: { "2885": {"scanner_confluence", "connors_rsi"}, ... }

        This is called once at scanner_warmup() and cached in AlgorithmManager.
        Deduplicates automatically — if the same stock appears in multiple
        watchlists bound to the same algorithm, it appears once.

        The live WebSocket parser and tick-routing pipeline currently key
        instruments by token and subscribe using SmartAPI's NSE exchange type.
        Keep BSE stocks in database-backed scanners, but out of this live-only
        route until that pipeline carries exchange identity end to end.

        Returns:
            Empty dict if no bindings exist (triggers fallback behavior).
        """
        rows = self._db.fetch_all(
            f"""
            SELECT DISTINCT s.token, wab.algorithm_name
            FROM watchlist_algorithm_bindings wab
            JOIN watchlists wl ON wl.id = wab.watchlist_id
            JOIN watchlist_items wi ON wi.watchlist_id = wl.id
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wab.is_active = TRUE
              AND wl.is_active = TRUE
              AND {equity_universe_sql('s')}
              AND s.exchange = 'NSE'
            ORDER BY s.token
            """
        )

        routing: dict[str, set[str]] = {}
        for row in rows:
            token = row["token"]
            algo = row["algorithm_name"]
            if token not in routing:
                routing[token] = set()
            routing[token].add(algo)

        logger.info(
            f"Built routing table: {len(routing)} tokens → "
            f"{len(set().union(*routing.values()) if routing else set())} algorithms"
        )
        return routing

    def get_subscribe_tokens(self) -> list[str]:
        """
        Get unique NSE tokens from active watchlists for WebSocket subscription.

        This is the union of NSE watchlist items across all active watchlists,
        regardless of algorithm bindings. BSE watchlist items remain available
        to database-backed screens; live WebSocket subscriptions are NSE-only
        until the tick pipeline carries exchange identity end to end.

        Returns:
            List of unique SmartAPI tokens. Falls back to empty list
            if no watchlists are configured.
        """
        rows = self._db.fetch_all(
            f"""
            SELECT DISTINCT s.token
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            JOIN watchlists wl ON wl.id = wi.watchlist_id
            WHERE wl.is_active = TRUE
              AND {equity_universe_sql('s')}
              AND s.exchange = 'NSE'
            ORDER BY s.token
            """
        )
        return [r["token"] for r in rows]

    def get_routing_summary(self) -> list[dict]:
        """
        Get a human-readable summary of all bindings for display.

        Returns list of {watchlist_name, algorithm_name, stock_count}.
        """
        return self._db.fetch_all(
            f"""
            SELECT
                wl.name AS watchlist_name,
                wab.algorithm_name,
                COUNT(DISTINCT wi.stock_id) FILTER (WHERE {equity_universe_sql('s')}) AS stock_count
            FROM watchlist_algorithm_bindings wab
            JOIN watchlists wl ON wl.id = wab.watchlist_id
            LEFT JOIN watchlist_items wi ON wi.watchlist_id = wl.id
            LEFT JOIN stocks s ON s.id = wi.stock_id
            WHERE wab.is_active = TRUE AND wl.is_active = TRUE
            GROUP BY wl.name, wab.algorithm_name
            ORDER BY wl.name, wab.algorithm_name
            """
        )


class CandleStore:
    """
    Generic CRUD for per-interval candle tables.

    Dynamically targets the correct table based on CandleInterval:
        candles_1m, candles_5m, candles_15m, candles_1h, candles_1d, candles_1w

    The daily table (candles_1d) has extra columns: delivery_qty, delivery_pct.

    Usage:
        store = CandleStore(db)
        store.insert(stock_id, ts, CandleInterval.FIVE_MINUTE, o, h, l, c, vol)
        rows = store.get_candles(stock_id, CandleInterval.ONE_DAY, days=90)
    """

    def __init__(self, db: Database):
        self._db = db

    @staticmethod
    def _table(interval):
        """Resolve interval enum to table name."""
        from core.constants import CANDLE_TABLE_SUFFIX
        suffix = CANDLE_TABLE_SUFFIX.get(interval)
        if not suffix:
            raise ValueError(f"Unknown candle interval: {interval}")
        return f"candles_{suffix}"

    # ── Writes ───────────────────────────────────────────

    def insert(
        self,
        stock_id: int,
        timestamp: datetime,
        interval,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        vwap: float = 0,
        delivery_qty: int = 0,
        delivery_pct: float = 0,
    ) -> None:
        """Insert or update a single candle."""
        from core.constants import CandleInterval as CI
        table = self._table(interval)

        if interval == CI.ONE_DAY:
            timestamp = _canonical_daily_timestamp(timestamp)
            self._db.execute(
                f"""
                INSERT INTO {table} (stock_id, timestamp, open, high, low, close,
                                     volume, vwap, delivery_qty, delivery_pct)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, timestamp) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
                    delivery_qty = EXCLUDED.delivery_qty,
                    delivery_pct = EXCLUDED.delivery_pct
                """,
                (stock_id, timestamp, open_, high, low, close,
                 volume, vwap, delivery_qty, delivery_pct),
            )
        else:
            self._db.execute(
                f"""
                INSERT INTO {table} (stock_id, timestamp, open, high, low, close,
                                     volume, vwap)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, timestamp) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, vwap = EXCLUDED.vwap
                """,
                (stock_id, timestamp, open_, high, low, close, volume, vwap),
            )

    def insert_batch(self, interval, rows: list[tuple]) -> int:
        """
        Batch upsert candles into the correct table.

        Args:
            interval: CandleInterval to determine the target table.
            rows: For daily: (stock_id, ts, open, high, low, close, vol, vwap, del_qty, del_pct).
                  For intraday: (stock_id, ts, open, high, low, close, vol, vwap).
        """
        from core.constants import CandleInterval as CI
        table = self._table(interval)

        if interval == CI.ONE_DAY:
            rows = [
                (row[0], _canonical_daily_timestamp(row[1]), *row[2:])
                for row in rows
            ]
            return self._db.execute_many(
                f"""
                INSERT INTO {table} (stock_id, timestamp, open, high, low, close,
                                     volume, vwap, delivery_qty, delivery_pct)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, timestamp) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
                    delivery_qty = EXCLUDED.delivery_qty,
                    delivery_pct = EXCLUDED.delivery_pct
                """,
                rows,
            )
        else:
            return self._db.execute_many(
                f"""
                INSERT INTO {table} (stock_id, timestamp, open, high, low, close,
                                     volume, vwap)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, timestamp) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close,
                    volume = EXCLUDED.volume, vwap = EXCLUDED.vwap
                """,
                rows,
            )

    def aggregate_complete_one_minute_groups(
        self,
        bars: list[tuple[int, date, dt_time]],
    ) -> list[tuple]:
        """Build missing five-minute edge bars from five complete 1-minute bars.

        The requested tuples are ``(stock_id, session_date, bar_start)`` in
        Asia/Kolkata. A result is returned only when every minute in the
        five-minute interval exists and the target five-minute candle is
        absent, so this never fills an edge with partial or inferred OHLCV.
        """
        if not bars:
            return []

        stock_ids, session_dates, bar_starts = zip(*bars)
        return self._db.fetch_all(
            """
            WITH requested AS (
                SELECT stock_id,
                       (session_date + bar_start) AT TIME ZONE 'Asia/Kolkata' AS bar_ts
                FROM UNNEST(%s::INTEGER[], %s::DATE[], %s::TIME[])
                     AS r(stock_id, session_date, bar_start)
            ), grouped AS (
                SELECT r.stock_id,
                       r.bar_ts,
                       (ARRAY_AGG(m.open ORDER BY m.timestamp ASC))[1] AS open,
                       MAX(m.high) AS high,
                       MIN(m.low) AS low,
                       (ARRAY_AGG(m.close ORDER BY m.timestamp DESC))[1] AS close,
                       SUM(m.volume)::BIGINT AS volume,
                       COALESCE(
                           SUM(m.vwap * m.volume) / NULLIF(SUM(m.volume), 0),
                           0
                       ) AS vwap,
                       COUNT(*) AS minute_count,
                       MIN(m.timestamp) AS first_minute,
                       MAX(m.timestamp) AS last_minute
                FROM requested r
                JOIN candles_1m m
                  ON m.stock_id = r.stock_id
                 AND m.timestamp >= r.bar_ts
                 AND m.timestamp < r.bar_ts + INTERVAL '5 minutes'
                WHERE NOT EXISTS (
                    SELECT 1 FROM candles_5m existing
                    WHERE existing.stock_id = r.stock_id
                      AND existing.timestamp = r.bar_ts
                )
                GROUP BY r.stock_id, r.bar_ts
            )
            SELECT stock_id, bar_ts AS timestamp, open, high, low, close,
                   volume, vwap
            FROM grouped
            WHERE minute_count = 5
              AND first_minute = bar_ts
              AND last_minute = bar_ts + INTERVAL '4 minutes'
            ORDER BY stock_id, bar_ts
            """,
            (list(stock_ids), list(session_dates), list(bar_starts)),
        )

    def aggregate_weekly_from_daily(
        self,
        *,
        days: int = 730,
        exchange: str = "NSE",
    ) -> int:
        """Upsert weekly candles in one database operation from daily candles."""
        return self._db.execute(
            f"""
            WITH daily AS (
                SELECT c.stock_id,
                       (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date AS session_date,
                       ((c.timestamp AT TIME ZONE 'Asia/Kolkata')::date
                           - (EXTRACT(ISODOW FROM (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date)::INT - 1)
                       ) AS week_start,
                       c.open,
                       c.high,
                       c.low,
                       c.close,
                       c.volume
                FROM candles_1d c
                JOIN stocks s ON s.id = c.stock_id
                WHERE {equity_universe_sql('s')}
                  AND s.exchange = %s
                  AND c.timestamp >= NOW() - (%s * INTERVAL '1 day')
            ), ranked AS (
                SELECT daily.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY stock_id, week_start ORDER BY session_date ASC
                       ) AS first_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY stock_id, week_start ORDER BY session_date DESC
                       ) AS last_rank
                FROM daily
            ), weekly AS (
                SELECT stock_id,
                       week_start,
                       MAX(open) FILTER (WHERE first_rank = 1) AS open,
                       MAX(high) AS high,
                       MIN(low) AS low,
                       MAX(close) FILTER (WHERE last_rank = 1) AS close,
                       SUM(volume)::BIGINT AS volume
                FROM ranked
                GROUP BY stock_id, week_start
            )
            INSERT INTO candles_1w (
                stock_id, timestamp, open, high, low, close, volume, vwap
            )
            SELECT stock_id,
                   week_start::TIMESTAMP AT TIME ZONE 'Asia/Kolkata',
                   open, high, low, close, volume, 0
            FROM weekly
            ON CONFLICT (stock_id, timestamp) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                vwap = EXCLUDED.vwap
            """,
            (exchange, days),
        )

    # ── Reads ────────────────────────────────────────────

    def get_candles(self, stock_id: int, interval, days: int = 90) -> list[dict]:
        """Fetch candles for a stock, most recent N days, sorted timestamp DESC."""
        table = self._table(interval)
        rows = self._db.fetch_all(
            f"""
            SELECT * FROM {table}
            WHERE stock_id = %s
              AND timestamp >= NOW() - INTERVAL '%s days'
            ORDER BY timestamp DESC
            """,
            (stock_id, days),
        )
        from core.constants import CandleInterval as CI
        if interval == CI.ONE_DAY:
            rows, _ = normalize_daily_candles(rows)
            rows.sort(key=lambda row: row["timestamp"], reverse=True)
        elif getattr(interval, "value", interval) in INTRADAY_TIMEFRAMES:
            rows = [row for row in rows if is_regular_session_candle(row["timestamp"])]
        return rows

    def get_latest_timestamp(self, stock_id: int, interval) -> datetime | None:
        """Get the most recent candle timestamp for incremental fetching."""
        table = self._table(interval)
        row = self._db.fetch_one(
            f"SELECT MAX(timestamp) AS latest_ts FROM {table} WHERE stock_id = %s",
            (stock_id,),
        )
        return row["latest_ts"] if row and row["latest_ts"] else None

    def get_avg_volume(self, stock_id: int, interval, days: int = 30) -> int:
        """Get average volume over N most recent candles."""
        table = self._table(interval)
        row = self._db.fetch_one(
            f"""
            SELECT COALESCE(AVG(volume), 0)::BIGINT AS avg_volume
            FROM (
                SELECT volume FROM {table}
                WHERE stock_id = %s
                ORDER BY timestamp DESC
                LIMIT %s
            ) sub
            """,
            (stock_id, days),
        )
        return row["avg_volume"] if row else 0

    def get_daily_aggregate(self, stock_id: int, source_interval, target_date: date) -> dict | None:
        """
        Aggregate intraday candles into a single daily candle.

        Returns: {open, high, low, close, volume} or None if no data.
        """
        table = self._table(source_interval)
        row = self._db.fetch_one(
            f"""
            SELECT
                (ARRAY_AGG(open ORDER BY timestamp ASC))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (ARRAY_AGG(close ORDER BY timestamp DESC))[1] AS close,
                SUM(volume)::BIGINT AS volume
            FROM {table}
            WHERE stock_id = %s AND timestamp::DATE = %s
            """,
            (stock_id, target_date),
        )
        if row and row["close"] is not None:
            return dict(row)
        return None

    def cleanup_old(self, interval, keep_days: int = 30) -> int:
        """Delete candles older than keep_days. Returns rows deleted."""
        table = self._table(interval)
        result = self._db.execute(
            f"DELETE FROM {table} WHERE timestamp < NOW() - INTERVAL '%s days'",
            (keep_days,),
        )
        if result > 0:
            logger.info(f"Cleaned up {result} old rows from {table}")
        return result

    # ── Bulk Queries (Pre-Market Warm-up) ────────────────

    def get_avg_volumes_bulk(self, days_30: int = 30, days_90: int = 90) -> dict[str, tuple[int, int]]:
        """
        Get 30d/90d average daily volumes for ALL active stocks from candles_1d.

        Returns: {token: (avg_30d, avg_90d)}.
        """
        rows_30 = self._db.fetch_all(
            f"""
            SELECT s.token, COALESCE(AVG(c.volume), 0)::BIGINT AS avg_volume
            FROM stocks s
            JOIN LATERAL (
                SELECT volume FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT %s
            ) c ON TRUE
            WHERE {equity_universe_sql('s')}
            GROUP BY s.token
            """,
            (days_30,),
        )
        avg_30 = {row["token"]: int(row["avg_volume"]) for row in rows_30}

        rows_90 = self._db.fetch_all(
            f"""
            SELECT s.token, COALESCE(AVG(c.volume), 0)::BIGINT AS avg_volume
            FROM stocks s
            JOIN LATERAL (
                SELECT volume FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT %s
            ) c ON TRUE
            WHERE {equity_universe_sql('s')}
            GROUP BY s.token
            """,
            (days_90,),
        )
        avg_90 = {row["token"]: int(row["avg_volume"]) for row in rows_90}

        all_tokens = set(avg_30.keys()) | set(avg_90.keys())
        result: dict[str, tuple[int, int]] = {}
        for token in all_tokens:
            v30 = avg_30.get(token, 0)
            v90 = avg_90.get(token, 0)
            if v30 > 0 or v90 > 0:
                result[token] = (v30, v90)

        logger.debug(f"Bulk avg volumes loaded for {len(result)} stocks")
        return result

    def get_daily_candles_bulk(self, days: int = 90) -> dict[int, list[dict]]:
        """
        Get daily candles for ALL active stocks from candles_1d.

        Returns: {stock_id: [candle_rows sorted by timestamp ASC]}
        """
        rows = self._db.fetch_all(
            """
            SELECT c.stock_id, c.timestamp, c.open, c.high, c.low, c.close,
                   c.volume, c.delivery_qty, c.delivery_pct, c.vwap
            FROM candles_1d c
            JOIN stocks s ON s.id = c.stock_id
            WHERE s.is_active = TRUE
              AND c.timestamp >= NOW() - INTERVAL '%s days'
            ORDER BY c.stock_id, c.timestamp ASC
            """,
            (days,),
        )
        rows, _ = normalize_daily_candles(rows)

        result: dict[int, list[dict]] = {}
        for row in rows:
            sid = row["stock_id"]
            if sid not in result:
                result[sid] = []
            result[sid].append(row)

        logger.debug(f"Bulk daily candles loaded for {len(result)} stocks")
        return result



class CandleCoverageStore:
    """
    Tracks what candle data we have for each stock in each interval.

    Each row = one stock + one interval, recording:
        - earliest_ts: oldest candle we have
        - latest_ts:   newest candle we have
        - candle_count: total rows stored
        - source:       where the data came from (angel_one, bhavcopy, aggregated)

    Usage:
        coverage = CandleCoverageStore(db)
        coverage.upsert(stock_id=1, interval="FIVE_MINUTE",
                        earliest=dt1, latest=dt2, count=500)
        gaps = coverage.find_gaps(interval="FIVE_MINUTE", required_days=90)
    """

    def __init__(self, db: Database):
        self._db = db

    def upsert(
        self,
        stock_id: int,
        interval: str,
        earliest: datetime,
        latest: datetime,
        candle_count: int = 0,
        source: str = "angel_one",
        history_complete: bool | None = None,
    ) -> None:
        """Insert or update coverage for a stock + interval."""
        self._db.execute(
            """
            INSERT INTO candle_coverage
                (stock_id, interval, earliest_ts, latest_ts, candle_count, source,
                 history_complete, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, FALSE), NOW())
            ON CONFLICT (stock_id, interval) DO UPDATE SET
                earliest_ts = LEAST(candle_coverage.earliest_ts, EXCLUDED.earliest_ts),
                latest_ts   = GREATEST(candle_coverage.latest_ts, EXCLUDED.latest_ts),
                candle_count = EXCLUDED.candle_count,
                source       = EXCLUDED.source,
                history_complete = candle_coverage.history_complete OR EXCLUDED.history_complete,
                last_updated = NOW()
            """,
            (stock_id, interval, earliest, latest, candle_count, source, history_complete),
        )

    def record_check(
        self,
        stock_id: int,
        interval: str,
        result: str,
        error: str = "",
    ) -> None:
        """Record a successful empty/data check or a final fetch error."""
        self._db.execute(
            """
            INSERT INTO candle_sync_checks
                (stock_id, interval, last_checked_at, last_result, last_error)
            VALUES (%s, %s, NOW(), %s, %s)
            ON CONFLICT (stock_id, interval) DO UPDATE SET
                last_checked_at = NOW(),
                last_result = EXCLUDED.last_result,
                last_error = EXCLUDED.last_error
            """,
            (stock_id, interval, result, error[:1000]),
        )

    def mark_history_complete(self, stock_id: int, interval: str) -> None:
        """Mark an older-boundary check complete even when it returned no rows."""
        self._db.execute(
            """
            UPDATE candle_coverage
            SET history_complete = TRUE,
                last_updated = NOW()
            WHERE stock_id = %s AND interval = %s
            """,
            (stock_id, interval),
        )

    def get_last_check(self, stock_id: int, interval: str) -> dict | None:
        """Return the most recent sync check for a stock + interval."""
        return self._db.fetch_one(
            """
            SELECT * FROM candle_sync_checks
            WHERE stock_id = %s AND interval = %s
            """,
            (stock_id, interval),
        )

    def get(self, stock_id: int, interval: str) -> dict | None:
        """Get coverage info for a specific stock + interval."""
        return self._db.fetch_one(
            """
            SELECT * FROM candle_coverage
            WHERE stock_id = %s AND interval = %s
            """,
            (stock_id, interval),
        )

    def get_all_for_interval(self, interval: str) -> list[dict]:
        """Get coverage for all stocks at a given interval."""
        return self._db.fetch_all(
            """
            SELECT cc.*, s.symbol, s.token
            FROM candle_coverage cc
            JOIN stocks s ON s.id = cc.stock_id
            WHERE cc.interval = %s
            ORDER BY s.symbol
            """,
            (interval,),
        )

    def get_summary(self) -> list[dict]:
        """
        Get a summary of coverage across all intervals.

        Returns one row per interval with stock count, date range, total candles.
        """
        return self._db.fetch_all(
            """
            SELECT
                interval,
                COUNT(*) AS stock_count,
                MIN(earliest_ts) AS global_earliest,
                MAX(latest_ts) AS global_latest,
                SUM(candle_count) AS total_candles
            FROM candle_coverage
            GROUP BY interval
            ORDER BY interval
            """,
        )

    def get_latest_date(self) -> date | None:
        """
        Get the most recent latest_ts date across all coverage entries.

        Used by the startup catch-up logic to determine if today's data
        has already been fetched.

        Returns:
            The date of the most recent candle coverage, or None if empty.
        """
        row = self._db.fetch_one(
            "SELECT MAX(latest_ts)::DATE AS latest_date FROM candle_coverage"
        )
        return row["latest_date"] if row and row["latest_date"] else None

    def find_gaps(
        self,
        interval: str,
        required_days: int = 90,
        exchange: str = "NSE",
    ) -> list[dict]:
        """
        Find stocks that are missing data or have less than required_days coverage.

        Returns list of {stock_id, symbol, token, earliest_ts, latest_ts, gap_days}
        for stocks that need backfilling. Also includes stocks with NO coverage at all.
        """
        return self._db.fetch_all(
            """
            SELECT
                s.id AS stock_id,
                s.symbol,
                s.token,
                cc.earliest_ts,
                cc.latest_ts,
                cc.candle_count,
                CASE
                    WHEN cc.id IS NULL THEN %s
                    ELSE GREATEST(0, %s - EXTRACT(DAY FROM (cc.latest_ts - cc.earliest_ts))::INT)
                END AS gap_days
            FROM stocks s
            LEFT JOIN candle_coverage cc ON cc.stock_id = s.id AND cc.interval = %s
            WHERE s.is_active = TRUE AND s.exchange = %s
              AND (
                  cc.id IS NULL
                  OR EXTRACT(DAY FROM (cc.latest_ts - cc.earliest_ts)) < %s
              )
            ORDER BY gap_days DESC
            """,
            (required_days, required_days, interval, exchange, required_days),
        )

    def find_stale(
        self,
        interval: str,
        stale_hours: int = 24,
    ) -> list[dict]:
        """
        Find stocks whose coverage hasn't been updated recently.

        Useful for nightly incremental updates — only fetch for stocks
        whose latest_ts is older than stale_hours ago.
        """
        return self._db.fetch_all(
            """
            SELECT cc.*, s.symbol, s.token
            FROM candle_coverage cc
            JOIN stocks s ON s.id = cc.stock_id
            WHERE cc.interval = %s
              AND cc.latest_ts < NOW() - INTERVAL '%s hours'
              AND s.is_active = TRUE
            ORDER BY cc.latest_ts ASC
            """,
            (interval, stale_hours),
        )

    def find_recent_session_edge_gaps(
        self,
        interval: str,
        start: datetime,
        end: datetime,
        exchange: str = "NSE",
        stock_ids: list[int] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Find stored intraday sessions missing their first or final full bar."""
        from core.constants import CANDLE_TABLE_SUFFIX, CandleInterval

        suffix = CANDLE_TABLE_SUFFIX.get(interval)
        if interval not in {
            CandleInterval.ONE_MINUTE.value,
            CandleInterval.FIVE_MINUTE.value,
            CandleInterval.FIFTEEN_MINUTE.value,
            CandleInterval.ONE_HOUR.value,
        } or not suffix:
            raise ValueError(f"Session-edge checks require an intraday interval, got {interval}")

        step = {
            CandleInterval.ONE_MINUTE.value: 1,
            CandleInterval.FIVE_MINUTE.value: 5,
            CandleInterval.FIFTEEN_MINUTE.value: 15,
            CandleInterval.ONE_HOUR.value: 60,
        }[interval]
        final_bar_time = (
            dt_time(15, 15)
            if interval == CandleInterval.ONE_HOUR.value
            else (datetime.combine(date.min, MARKET_CLOSE) - timedelta(minutes=step)).time()
        )

        stock_filter = ""
        if stock_ids is not None and not stock_ids:
            return []
        params: list[Any] = [exchange, start, end, MARKET_OPEN, final_bar_time]
        if stock_ids is not None:
            stock_filter = "AND c.stock_id = ANY(%s)"
            params.append(stock_ids)
        params.extend((MARKET_OPEN, final_bar_time))

        query = f"""
            WITH local_candles AS (
                SELECT c.stock_id, s.symbol, s.token,
                       (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date AS session_date,
                       (c.timestamp AT TIME ZONE 'Asia/Kolkata')::time AS session_time
                FROM candles_{suffix} c
                JOIN stocks s ON s.id = c.stock_id
                WHERE s.is_active = TRUE
                  AND s.exchange = %s
                  AND {equity_universe_sql('s')}
                  AND c.timestamp >= %s
                  AND c.timestamp < %s
                  AND (c.timestamp AT TIME ZONE 'Asia/Kolkata')::time BETWEEN %s AND %s
                  AND EXTRACT(ISODOW FROM (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date) BETWEEN 1 AND 5
                  {stock_filter}
            ), sessions AS (
                SELECT stock_id, symbol, token, session_date,
                       BOOL_OR(session_time = %s) AS has_open,
                       BOOL_OR(session_time = %s) AS has_close
                FROM local_candles
                GROUP BY stock_id, symbol, token, session_date
            )
            SELECT stock_id, symbol, token, session_date, has_open, has_close
            FROM sessions
            WHERE NOT has_open OR NOT has_close
            ORDER BY session_date DESC, symbol
        """
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        return self._db.fetch_all(query, tuple(params))

    def refresh_stock_from_table(self, stock_id: int, interval: str) -> None:
        """Reconcile one stock's coverage after a targeted candle repair."""
        from core.constants import CANDLE_TABLE_SUFFIX

        suffix = CANDLE_TABLE_SUFFIX.get(interval)
        if not suffix:
            raise ValueError(f"Unknown interval: {interval}")
        row = self._db.fetch_one(
            f"""
            SELECT MIN(timestamp) AS earliest_ts, MAX(timestamp) AS latest_ts,
                   COUNT(*) AS candle_count
            FROM candles_{suffix}
            WHERE stock_id = %s
            """,
            (stock_id,),
        )
        if row and row.get("earliest_ts") and row.get("latest_ts"):
            self.upsert(
                stock_id=stock_id,
                interval=interval,
                earliest=row["earliest_ts"],
                latest=row["latest_ts"],
                candle_count=row["candle_count"],
                source="reconciled",
            )

    def refresh_from_table(self, interval: str) -> int:
        """
        Recompute coverage from actual candle data in the table.

        Useful after manual imports or if coverage gets out of sync.
        Scans the actual candle table and updates candle_coverage accordingly.
        """
        from core.constants import CANDLE_TABLE_SUFFIX
        suffix = CANDLE_TABLE_SUFFIX.get(interval)
        if not suffix:
            raise ValueError(f"Unknown interval: {interval}")
        table = f"candles_{suffix}"

        result = self._db.execute(
            f"""
            INSERT INTO candle_coverage (stock_id, interval, earliest_ts, latest_ts, candle_count, source, last_updated)
            SELECT
                stock_id,
                %s AS interval,
                MIN(timestamp) AS earliest_ts,
                MAX(timestamp) AS latest_ts,
                COUNT(*) AS candle_count,
                'refreshed' AS source,
                NOW() AS last_updated
            FROM {table}
            GROUP BY stock_id
            ON CONFLICT (stock_id, interval) DO UPDATE SET
                earliest_ts  = EXCLUDED.earliest_ts,
                latest_ts    = EXCLUDED.latest_ts,
                candle_count = EXCLUDED.candle_count,
                last_updated = NOW()
            """,
            (interval,),
        )
        logger.info(f"Coverage refreshed for {table}: {result} stocks updated")
        return result


class SignalStore:
    """CRUD operations for the signals table."""

    def __init__(self, db: Database):
        self._db = db

    def insert(self, stock_id: int, signal_type: str, strength: str,
               score: float, price: float, sector_id: int | None = None,
               analyzer_name: str = "", analyzer_confidence: float = 0,
               analyzer_reasoning: str = "", volume_current: int = 0,
               volume_avg: int = 0, volume_ratio: float = 0,
               metadata: dict | None = None) -> int | None:
        import json
        row = self._db.execute_returning(
            """
            INSERT INTO signals (stock_id, sector_id, signal_type, strength, score,
                                 analyzer_name, analyzer_confidence, analyzer_reasoning,
                                 volume_current, volume_avg, volume_ratio, price, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (stock_id, sector_id, signal_type, strength, score,
             analyzer_name, analyzer_confidence, analyzer_reasoning,
             volume_current, volume_avg, volume_ratio, price,
             json.dumps(metadata or {})),
        )
        return row["id"] if row else None

    def update_status(self, signal_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE signals SET status = %s WHERE id = %s",
            (status, signal_id),
        )

    def get_recent(self, limit: int = 50) -> list[dict]:
        return self._db.fetch_all(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    def get_today(self) -> list[dict]:
        return self._db.fetch_all(
            "SELECT * FROM signals WHERE created_at::DATE = CURRENT_DATE ORDER BY created_at DESC"
        )


class OrderStore:
    """CRUD operations for the orders table."""

    def __init__(self, db: Database):
        self._db = db

    def insert(self, signal_id: int | None, stock_id: int, order_type: str,
               product_type: str, transaction_type: str, quantity: int,
               price: float, is_paper: bool = True,
               trigger_price: float = 0) -> int | None:
        row = self._db.execute_returning(
            """
            INSERT INTO orders (signal_id, stock_id, order_type, product_type,
                               transaction_type, quantity, price, trigger_price,
                               is_paper_trade)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (signal_id, stock_id, order_type, product_type,
             transaction_type, quantity, price, trigger_price, is_paper),
        )
        return row["id"] if row else None

    def update_fill(self, order_id: int, status: str, filled_qty: int,
                    avg_fill_price: float, broker_order_id: str = "") -> None:
        self._db.execute(
            """
            UPDATE orders SET
                status = %s, filled_qty = %s, avg_fill_price = %s,
                broker_order_id = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (status, filled_qty, avg_fill_price, broker_order_id, order_id),
        )

    def get_today(self, is_paper: bool = True) -> list[dict]:
        return self._db.fetch_all(
            """
            SELECT * FROM orders
            WHERE created_at::DATE = CURRENT_DATE AND is_paper_trade = %s
            ORDER BY created_at DESC
            """,
            (is_paper,),
        )


class DailyPnLStore:
    """CRUD operations for the daily_pnl table."""

    def __init__(self, db: Database):
        self._db = db

    def upsert(self, pnl_date: date, realized: float, unrealized: float,
               total_trades: int, winning: int, losing: int,
               max_drawdown: float, portfolio_value: float,
               cash_balance: float, is_paper: bool = True) -> None:
        self._db.execute(
            """
            INSERT INTO daily_pnl (date, realized_pnl, unrealized_pnl, total_trades,
                                   winning_trades, losing_trades, max_drawdown,
                                   portfolio_value, cash_balance, is_paper)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, is_paper) DO UPDATE SET
                realized_pnl = EXCLUDED.realized_pnl,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                total_trades = EXCLUDED.total_trades,
                winning_trades = EXCLUDED.winning_trades,
                losing_trades = EXCLUDED.losing_trades,
                max_drawdown = EXCLUDED.max_drawdown,
                portfolio_value = EXCLUDED.portfolio_value,
                cash_balance = EXCLUDED.cash_balance
            """,
            (pnl_date, realized, unrealized, total_trades, winning, losing,
             max_drawdown, portfolio_value, cash_balance, is_paper),
        )

    def get_recent(self, days: int = 30, is_paper: bool = True) -> list[dict]:
        return self._db.fetch_all(
            """
            SELECT * FROM daily_pnl
            WHERE is_paper = %s
            ORDER BY date DESC
            LIMIT %s
            """,
            (is_paper, days),
        )

    def get_total_pnl(self, is_paper: bool = True) -> float:
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM daily_pnl WHERE is_paper = %s",
            (is_paper,),
        )
        return float(row["total"]) if row else 0.0


class AuditLogStore:
    """CRUD operations for the audit_log table."""

    def __init__(self, db: Database):
        self._db = db

    def insert(self, entry: dict[str, Any]) -> None:
        """Insert an audit log entry from the AuditTrail system."""
        import json
        self._db.execute(
            """
            INSERT INTO audit_log (timestamp, event_type, details, ip_address,
                                   checksum, prev_checksum)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                entry.get("timestamp"),
                entry.get("event_type"),
                json.dumps(entry.get("details", {})),
                entry.get("ip_address", ""),
                entry.get("checksum", ""),
                entry.get("prev_checksum", ""),
            ),
        )

    def get_recent(self, limit: int = 100, event_type: str | None = None) -> list[dict]:
        if event_type:
            return self._db.fetch_all(
                "SELECT * FROM audit_log WHERE event_type = %s ORDER BY timestamp DESC LIMIT %s",
                (event_type, limit),
            )
        return self._db.fetch_all(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )


class ScannerSignalStore:
    """
    CRUD operations for the scanner_signals table.

    Persists post-market batch scanner results (delivery, pattern, MTF)
    so algorithms can pre-seed their watchlists at morning warm-up.

    Lifecycle:
        post_market() → insert_batch()     [4:00 PM]
        scanner_warmup() → get_active()     [9:00 AM next day]
        scanner_warmup() → mark_consumed()  [after feeding to algorithms]
        midnight_reset() → expire_old()     [cleanup]
    """

    def __init__(self, db: Database):
        self._db = db

    def insert_batch(
        self,
        signals: list[dict],
        scan_date: date,
        target_date: date,
    ) -> int:
        """
        Bulk insert scanner signals from post-market batch scanners.

        Args:
            signals: List of dicts with keys:
                symbol, token, scanner_name, pattern, direction,
                confidence, price, strength, details
            scan_date: Date the scan was run (today).
            target_date: Date the signals are for (next trading day).

        Returns:
            Number of signals stored.
        """
        import json

        if not signals:
            return 0

        stored = 0
        for sig in signals:
            # Resolve stock_id from symbol
            stock_row = self._db.fetch_one(
                f"SELECT id FROM stocks WHERE symbol = %s AND {equity_universe_sql('stocks')} LIMIT 1",
                (sig["symbol"],),
            )
            if not stock_row:
                continue

            try:
                self._db.execute(
                    """
                    INSERT INTO scanner_signals
                        (stock_id, scan_date, target_date, scanner_name,
                         pattern, direction, confidence, price, strength, details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (stock_id, scan_date, scanner_name, pattern) DO UPDATE SET
                        confidence = EXCLUDED.confidence,
                        price = EXCLUDED.price,
                        direction = EXCLUDED.direction,
                        details = EXCLUDED.details,
                        status = 'ACTIVE'
                    """,
                    (
                        stock_row["id"], scan_date, target_date,
                        sig["scanner_name"], sig["pattern"],
                        sig["direction"], sig["confidence"],
                        sig["price"], sig["strength"],
                        json.dumps(sig.get("details", {})),
                    ),
                )
                stored += 1
            except Exception as e:
                logger.debug(f"Scanner signal insert failed for {sig.get('symbol')}: {e}")

        return stored

    def get_active_for_date(self, target_date: date) -> list[dict]:
        """
        Load all ACTIVE scanner signals for a specific target date.

        Called during scanner_warmup() to pre-seed algorithms.

        Returns:
            List of dicts with signal data + stock symbol/token.
        """
        return self._db.fetch_all(
            f"""
            SELECT ss.id, ss.scanner_name, ss.pattern, ss.direction,
                   ss.confidence, ss.price, ss.strength, ss.details,
                   s.symbol, s.token
            FROM scanner_signals ss
            JOIN stocks s ON s.id = ss.stock_id
            WHERE ss.target_date = %s AND ss.status = 'ACTIVE'
              AND {equity_universe_sql('s')}
            ORDER BY ss.confidence DESC
            """,
            (target_date,),
        )

    def mark_consumed(self, signal_ids: list[int]) -> int:
        """Mark signals as consumed after feeding to algorithms."""
        if not signal_ids:
            return 0
        placeholders = ",".join(["%s"] * len(signal_ids))
        return self._db.execute(
            f"UPDATE scanner_signals SET status = 'CONSUMED' WHERE id IN ({placeholders})",
            tuple(signal_ids),
        )

    def expire_old(self, before_date: date) -> int:
        """Expire signals older than the given date."""
        return self._db.execute(
            """
            UPDATE scanner_signals SET status = 'EXPIRED'
            WHERE target_date < %s AND status = 'ACTIVE'
            """,
            (before_date,),
        )

    def get_stats(self, target_date: date) -> dict:
        """Get signal counts by scanner and status for a date."""
        rows = self._db.fetch_all(
            """
            SELECT scanner_name, status, COUNT(*) as cnt
            FROM scanner_signals
            WHERE target_date = %s
            GROUP BY scanner_name, status
            ORDER BY scanner_name
            """,
            (target_date,),
        )
        return {
            f"{r['scanner_name']}_{r['status']}": r["cnt"]
            for r in rows
        }

class SectorStore:
    """CRUD operations for the sectors table."""

    def __init__(self, db: Database):
        self._db = db

    def upsert(self, name: str, display_name: str, nifty_index: str = "") -> int | None:
        row = self._db.execute_returning(
            """
            INSERT INTO sectors (name, display_name, nifty_index)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                nifty_index = EXCLUDED.nifty_index
            RETURNING id
            """,
            (name, display_name, nifty_index),
        )
        return row["id"] if row else None

    def get_all(self) -> list[dict]:
        return self._db.fetch_all(
            "SELECT * FROM sectors WHERE is_active = TRUE ORDER BY name"
        )

    def get_by_name(self, name: str) -> dict | None:
        return self._db.fetch_one(
            "SELECT * FROM sectors WHERE name = %s", (name,),
        )


class TickStore:
    """
    CRUD operations for the live_ticks table.

    Designed for high-volume batched inserts. Never insert one tick
    at a time — always use insert_batch().
    """

    def __init__(self, db: Database):
        self._db = db

    def insert_batch(self, ticks: list[tuple]) -> int:
        """
        Batch insert raw ticks.

        Args:
            ticks: List of (stock_id, token, timestamp, ltp, volume,
                   open, high, low, best_bid, best_ask, avg_trade_price).

        Returns:
            Number of rows inserted.
        """
        return self._db.execute_many(
            """
            INSERT INTO live_ticks (stock_id, token, timestamp, ltp, volume,
                                     open, high, low, best_bid, best_ask,
                                     avg_trade_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ticks,
        )

    def get_by_token(self, token: str, limit: int = 1000) -> list[dict]:
        """Get recent ticks for a specific token."""
        return self._db.fetch_all(
            "SELECT * FROM live_ticks WHERE token = %s ORDER BY timestamp DESC LIMIT %s",
            (token, limit),
        )

    def get_by_time_range(self, token: str, start: Any, end: Any) -> list[dict]:
        """Get ticks for a token within a time range."""
        return self._db.fetch_all(
            """
            SELECT * FROM live_ticks
            WHERE token = %s AND timestamp >= %s AND timestamp <= %s
            ORDER BY timestamp ASC
            """,
            (token, start, end),
        )

    def purge_older_than(self, days: int = 3) -> int:
        """
        Delete ticks older than N days.

        Returns:
            Number of rows deleted.
        """
        result = self._db.execute_returning(
            """
            WITH deleted AS (
                DELETE FROM live_ticks
                WHERE timestamp < NOW() - INTERVAL '%s days'
                RETURNING id
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (days,),
        )
        count = result["count"] if result else 0
        logger.info(f"Purged {count} ticks older than {days} days")
        return count

    def get_row_count(self) -> int:
        """Get total rows in live_ticks (for monitoring)."""
        row = self._db.fetch_one("SELECT COUNT(*) AS count FROM live_ticks")
        return row["count"] if row else 0


class TechnicalIndicatorStore:
    """
    CRUD operations for the technical_indicators table.

    Stores EOD-computed indicators for every stock. Written post-market,
    read during warm-up and optionally on the hot path.
    """

    def __init__(self, db: Database):
        self._db = db

    # All columns in insertion order (must match upsert SQL)
    _COLUMNS = (
        "stock_id", "date", "rsi_14", "ema_20", "ema_50", "atr_14",
        "macd_line", "macd_signal", "delivery_pct",
        "sma_20", "sma_50", "sma_200", "macd_histogram",
        "bollinger_upper", "bollinger_lower", "bollinger_width",
        "supertrend", "supertrend_direction", "adx_14", "obv",
        "pivot_point", "pivot_r1", "pivot_r2", "pivot_s1", "pivot_s2",
        "support_level", "resistance_level", "high_52w", "low_52w", "vwap_daily",
    )

    def upsert(self, indicators) -> None:
        """
        Insert or update a single stock's indicators for a date.

        Args:
            indicators: TechnicalIndicators dataclass instance.
        """
        self._db.execute(
            """
            INSERT INTO technical_indicators (
                stock_id, date, rsi_14, ema_20, ema_50, atr_14,
                macd_line, macd_signal, delivery_pct,
                sma_20, sma_50, sma_200, macd_histogram,
                bollinger_upper, bollinger_lower, bollinger_width,
                supertrend, supertrend_direction, adx_14, obv,
                pivot_point, pivot_r1, pivot_r2, pivot_s1, pivot_s2,
                support_level, resistance_level, high_52w, low_52w, vwap_daily
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (stock_id, date) DO UPDATE SET
                rsi_14 = EXCLUDED.rsi_14,
                ema_20 = EXCLUDED.ema_20,
                ema_50 = EXCLUDED.ema_50,
                atr_14 = EXCLUDED.atr_14,
                macd_line = EXCLUDED.macd_line,
                macd_signal = EXCLUDED.macd_signal,
                delivery_pct = EXCLUDED.delivery_pct,
                sma_20 = EXCLUDED.sma_20,
                sma_50 = EXCLUDED.sma_50,
                sma_200 = EXCLUDED.sma_200,
                macd_histogram = EXCLUDED.macd_histogram,
                bollinger_upper = EXCLUDED.bollinger_upper,
                bollinger_lower = EXCLUDED.bollinger_lower,
                bollinger_width = EXCLUDED.bollinger_width,
                supertrend = EXCLUDED.supertrend,
                supertrend_direction = EXCLUDED.supertrend_direction,
                adx_14 = EXCLUDED.adx_14,
                obv = EXCLUDED.obv,
                pivot_point = EXCLUDED.pivot_point,
                pivot_r1 = EXCLUDED.pivot_r1,
                pivot_r2 = EXCLUDED.pivot_r2,
                pivot_s1 = EXCLUDED.pivot_s1,
                pivot_s2 = EXCLUDED.pivot_s2,
                support_level = EXCLUDED.support_level,
                resistance_level = EXCLUDED.resistance_level,
                high_52w = EXCLUDED.high_52w,
                low_52w = EXCLUDED.low_52w,
                vwap_daily = EXCLUDED.vwap_daily
            """,
            (
                indicators.stock_id, indicators.date,
                indicators.rsi_14, indicators.ema_20, indicators.ema_50,
                indicators.atr_14, indicators.macd_line, indicators.macd_signal,
                indicators.delivery_pct,
                indicators.sma_20, indicators.sma_50, indicators.sma_200,
                indicators.macd_histogram,
                indicators.bollinger_upper, indicators.bollinger_lower,
                indicators.bollinger_width,
                indicators.supertrend, indicators.supertrend_direction,
                indicators.adx_14, indicators.obv,
                indicators.pivot_point, indicators.pivot_r1, indicators.pivot_r2,
                indicators.pivot_s1, indicators.pivot_s2,
                indicators.support_level, indicators.resistance_level,
                indicators.high_52w, indicators.low_52w, indicators.vwap_daily,
            ),
        )

    def upsert_batch(self, indicators_list: list) -> int:
        """
        Batch upsert indicators for multiple stocks.

        Args:
            indicators_list: List of TechnicalIndicators dataclass instances.

        Returns:
            Number of rows upserted.
        """
        count = 0
        for ind in indicators_list:
            try:
                self.upsert(ind)
                count += 1
            except Exception as e:
                logger.error(f"Failed to upsert indicators for stock_id={ind.stock_id}: {e}")
        return count

    def get_latest(self, stock_id: int) -> dict | None:
        """Get the most recent indicator row for a stock."""
        return self._db.fetch_one(
            """
            SELECT * FROM technical_indicators
            WHERE stock_id = %s
            ORDER BY date DESC
            LIMIT 1
            """,
            (stock_id,),
        )

    def get_for_date(self, stock_id: int, target_date) -> dict | None:
        """Get indicator row for a specific stock and date."""
        return self._db.fetch_one(
            """
            SELECT * FROM technical_indicators
            WHERE stock_id = %s AND date = %s
            """,
            (stock_id, target_date),
        )

    def get_bulk_latest(self, stock_ids: list[int] | None = None) -> list[dict]:
        """
        Get the latest indicator row for each stock.

        Uses DISTINCT ON to get one row per stock_id (the most recent).
        If stock_ids is None, returns for ALL stocks.

        Args:
            stock_ids: Optional list of stock IDs to filter.

        Returns:
            List of indicator dicts, one per stock.
        """
        if stock_ids:
            placeholders = ",".join(["%s"] * len(stock_ids))
            return self._db.fetch_all(
                f"""
                SELECT DISTINCT ON (stock_id) *
                FROM technical_indicators
                WHERE stock_id IN ({placeholders})
                ORDER BY stock_id, date DESC
                """,
                tuple(stock_ids),
            )
        else:
            return self._db.fetch_all(
                """
                SELECT DISTINCT ON (stock_id) *
                FROM technical_indicators
                ORDER BY stock_id, date DESC
                """
            )

    def get_count(self, target_date=None) -> int:
        """Get total indicator rows (optionally for a specific date)."""
        if target_date:
            row = self._db.fetch_one(
                "SELECT COUNT(*) AS count FROM technical_indicators WHERE date = %s",
                (target_date,),
            )
        else:
            row = self._db.fetch_one(
                "SELECT COUNT(*) AS count FROM technical_indicators"
            )
        return row["count"] if row else 0


class FilterProfileStore:
    """Persistence for reusable filter profiles, immutable published versions, and bindings."""

    def __init__(self, db: Database):
        self._db = db

    def create_profile(self, *, name: str, description: str = "", mode: str = "screen", owner_id: str | None = None, expression: dict | None = None, requirements: dict | None = None, risk_config: dict | None = None, timeframe_config: dict | None = None) -> dict:
        profile = self._db.execute_returning(
            "INSERT INTO filter_profiles (name, description, mode, owner_id, is_active) VALUES (%s, %s, %s, %s, TRUE) RETURNING *",
            (name, description, mode, owner_id),
        )
        if not profile:
            raise RuntimeError("filter profile was not created")
        version = self._db.execute_returning(
            """
            INSERT INTO filter_profile_versions
                (profile_id, version_number, status, schema_version, expression_json, requirements_json, risk_config_json, timeframe_config_json)
            VALUES (%s, 1, 'draft', 1, %s, %s, %s, %s)
            RETURNING *
            """,
            (profile["id"], Json(expression or {}), Json(requirements or {}), Json(risk_config or {}), Json(timeframe_config or {})),
        )
        profile["latest_version"] = version
        return profile

    def get_profile(self, profile_id: int) -> dict | None:
        profile = self._db.fetch_one("SELECT * FROM filter_profiles WHERE id = %s", (profile_id,))
        if not profile:
            return None
        profile["versions"] = self._db.fetch_all(
            "SELECT * FROM filter_profile_versions WHERE profile_id = %s ORDER BY version_number DESC",
            (profile_id,),
        )
        return profile

    def list_profiles(self, *, active_only: bool = True) -> list[dict]:
        where = "WHERE is_active = TRUE" if active_only else ""
        return self._db.fetch_all(f"SELECT * FROM filter_profiles {where} ORDER BY updated_at DESC, id DESC")

    def get_version(self, profile_id: int, version_number: int | None = None) -> dict | None:
        if version_number is None:
            return self._db.fetch_one("SELECT * FROM filter_profile_versions WHERE profile_id = %s ORDER BY version_number DESC LIMIT 1", (profile_id,))
        return self._db.fetch_one("SELECT * FROM filter_profile_versions WHERE profile_id = %s AND version_number = %s", (profile_id, version_number))

    def update_draft(self, *, profile_id: int, expression: dict, requirements: dict, risk_config: dict | None = None, timeframe_config: dict | None = None, expected_version_id: int | None = None) -> dict:
        draft = self._db.fetch_one("SELECT * FROM filter_profile_versions WHERE profile_id = %s AND status = 'draft' ORDER BY version_number DESC LIMIT 1", (profile_id,))
        if draft:
            if expected_version_id is not None and draft["id"] != expected_version_id:
                raise ValueError("draft version conflict")
            updated = self._db.execute_returning(
                """
                UPDATE filter_profile_versions
                SET expression_json = %s, requirements_json = %s, risk_config_json = %s, timeframe_config_json = %s, updated_at = NOW()
                WHERE id = %s AND status = 'draft'
                RETURNING *
                """,
                (Json(expression), Json(requirements), Json(risk_config or {}), Json(timeframe_config or {}), draft["id"]),
            )
            if not updated:
                raise ValueError("draft version conflict")
            return updated
        latest = self._db.fetch_one("SELECT version_number FROM filter_profile_versions WHERE profile_id = %s ORDER BY version_number DESC LIMIT 1", (profile_id,))
        next_version = int(latest["version_number"]) + 1 if latest else 1
        return self._db.execute_returning(
            """
            INSERT INTO filter_profile_versions
                (profile_id, version_number, status, schema_version, expression_json, requirements_json, risk_config_json, timeframe_config_json)
            VALUES (%s, %s, 'draft', 1, %s, %s, %s, %s)
            RETURNING *
            """,
            (profile_id, next_version, Json(expression), Json(requirements), Json(risk_config or {}), Json(timeframe_config or {})),
        )

    def publish(self, *, profile_id: int, version_id: int) -> dict:
        version = self._db.fetch_one("SELECT * FROM filter_profile_versions WHERE id = %s AND profile_id = %s", (version_id, profile_id))
        if not version or version.get("status") != "draft":
            raise ValueError("only a draft version can be published")
        self._db.execute("UPDATE filter_profile_versions SET status = 'archived' WHERE profile_id = %s AND status = 'published' AND id <> %s", (profile_id, version_id))
        published = self._db.execute_returning("UPDATE filter_profile_versions SET status = 'published', published_at = NOW(), updated_at = NOW() WHERE id = %s AND status = 'draft' RETURNING *", (version_id,))
        if not published:
            raise ValueError("draft version conflict")
        return published

    def archive_profile(self, profile_id: int) -> bool:
        return self._db.execute("UPDATE filter_profiles SET is_active = FALSE, updated_at = NOW() WHERE id = %s", (profile_id,)) > 0

    def upsert_binding(self, *, profile_id: int, version_id: int, target_type: str, target_id: str, direction: str = "BUY", enabled: bool = True) -> dict:
        version = self._db.fetch_one("SELECT status FROM filter_profile_versions WHERE id = %s AND profile_id = %s", (version_id, profile_id))
        if not version or version.get("status") != "published":
            raise ValueError("automated bindings require a published profile version")
        return self._db.execute_returning(
            """
            INSERT INTO filter_profile_bindings (profile_id, version_id, target_type, target_id, direction, enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (target_type, target_id, direction)
            DO UPDATE SET profile_id = EXCLUDED.profile_id, version_id = EXCLUDED.version_id, enabled = EXCLUDED.enabled, updated_at = NOW()
            RETURNING *
            """,
            (profile_id, version_id, target_type, target_id, direction, enabled),
        )

    def get_bindings(self, profile_id: int) -> list[dict]:
        return self._db.fetch_all("SELECT * FROM filter_profile_bindings WHERE profile_id = %s ORDER BY target_type, target_id, direction", (profile_id,))


class TrendlineStore:
    """CRUD for user-drawn, symbol/timeframe-scoped trendlines."""

    def __init__(self, db: Database):
        self._db = db

    def create(
        self,
        symbol: str,
        interval: str,
        points: list[dict],
        trendline_type: str = "support",
        source: str = "manual",
        name: str | None = None,
    ) -> dict:
        if len(points) < 2:
            raise ValueError("a trendline requires at least two points")
        if any("time" not in point or "price" not in point for point in points):
            raise ValueError("each trendline point requires time and price")
        return self._db.execute_returning(
            """
            INSERT INTO trendline_definitions
                (symbol, interval, points_json, trendline_type, source, name, active)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            RETURNING *
            """,
            (symbol.upper(), interval, Json(points), trendline_type, source, name),
        )

    def list(self, symbol: str, interval: str, active_only: bool = True) -> list[dict]:
        active_clause = " AND active=TRUE" if active_only else ""
        return self._db.fetch_all(
            "SELECT * FROM trendline_definitions WHERE UPPER(symbol)=UPPER(%s) AND interval=%s"
            + active_clause
            + " ORDER BY id",
            (symbol, interval),
        )

    def update(self, trendline_id: int, points: list[dict], active: bool = True) -> dict | None:
        if len(points) < 2:
            raise ValueError("a trendline requires at least two points")
        return self._db.execute_returning(
            "UPDATE trendline_definitions SET points_json=%s, active=%s, updated_at=NOW() WHERE id=%s RETURNING *",
            (Json(points), active, trendline_id),
        )

    def delete(self, trendline_id: int) -> None:
        self._db.execute(
            "UPDATE trendline_definitions SET active=FALSE, updated_at=NOW() WHERE id=%s",
            (trendline_id,),
        )


class FundamentalStore:
    """Persistence boundary for normalized and raw fundamentals data."""

    SOURCE = "upstox"
    SYNC_CYCLE_ADVISORY_LOCK_KEY = 8_620_402

    def __init__(self, db: Database):
        self._db = db

    @contextmanager
    def sync_cycle_lock(self, *, wait: bool = False):
        """Serialize complete API cycles across app workers and CLI runs."""
        with self._db.get_connection() as conn:
            acquired = False
            with conn.cursor() as cur:
                if wait:
                    cur.execute("SELECT pg_advisory_lock(%s)", (self.SYNC_CYCLE_ADVISORY_LOCK_KEY,))
                    acquired = True
                else:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (self.SYNC_CYCLE_ADVISORY_LOCK_KEY,))
                    acquired = bool(cur.fetchone()[0])
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (self.SYNC_CYCLE_ADVISORY_LOCK_KEY,))
                    cur.fetchone()

    def rate_limit_cooldown_seconds(self, interval_seconds: int) -> float:
        """Return remaining post-batch cooldown, including after a restart."""
        row = self._db.fetch_one(
            """
            SELECT GREATEST(
                0,
                EXTRACT(EPOCH FROM (
                    finished_at + (%s * INTERVAL '1 second') - NOW()
                ))
            ) AS remaining_seconds
            FROM fundamental_sync_runs
            WHERE finished_at IS NOT NULL
              AND COALESCE(successful_requests, 0) + COALESCE(failed_requests, 0) > 0
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (interval_seconds,),
        )
        return max(0.0, float(row["remaining_seconds"] or 0)) if row else 0.0

    def start_run(self, requested_stocks: int) -> int | None:
        # Serialize run creation across API workers, the CLI and any other
        # local process. A committed running row is the durable lease.
        with self._db.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (8_620_401,))
            cur.execute(
                """
                UPDATE fundamental_sync_runs
                SET status = 'failed', finished_at = NOW(),
                    error_message = 'Marked stale after process stopped'
                WHERE status = 'running'
                  AND started_at < NOW() - INTERVAL '2 hours'
                """
            )
            cur.execute(
                "SELECT id FROM fundamental_sync_runs WHERE status = 'running' LIMIT 1"
            )
            if cur.fetchone():
                return None
            cur.execute(
                """
                INSERT INTO fundamental_sync_runs (requested_stocks)
                VALUES (%s)
                RETURNING id
                """,
                (requested_stocks,),
            )
            row = cur.fetchone()
            return row["id"] if row else None

    def finish_run(
        self,
        run_id: int | None,
        *,
        status: str,
        processed_stocks: int,
        successful_requests: int,
        failed_requests: int,
        skipped_stocks: int,
        details: dict,
        error_message: str = "",
    ) -> None:
        if not run_id:
            return
        self._db.execute(
            """
            UPDATE fundamental_sync_runs
            SET finished_at = NOW(), status = %s, processed_stocks = %s,
                successful_requests = %s, failed_requests = %s,
                skipped_stocks = %s, details = %s, error_message = %s
            WHERE id = %s
            """,
            (
                status,
                processed_stocks,
                successful_requests,
                failed_requests,
                skipped_stocks,
                Json(details),
                error_message,
                run_id,
            ),
        )

    def get_due_stocks(self, endpoint: str, cutoff: datetime, limit: int = 0) -> list[dict]:
        query = f"""
            SELECT s.id, s.symbol, s.name, s.exchange, s.series,
                   s.isin, s.upstox_instrument_key
            FROM stocks s
            LEFT JOIN fundamental_sync_state fs
              ON fs.stock_id = s.id AND fs.endpoint = %s
            WHERE {equity_universe_sql('s')}
              AND s.isin IS NOT NULL
              AND s.isin <> ''
              AND (fs.next_due_at IS NULL OR fs.next_due_at <= NOW())
              AND (fs.last_success_at IS NULL OR fs.last_success_at <= %s)
            ORDER BY fs.last_attempted_at ASC NULLS FIRST, s.symbol
        """
        if limit > 0:
            query += " LIMIT %s"
            return self._db.fetch_all(query, (endpoint, cutoff, limit))
        return self._db.fetch_all(query, (endpoint, cutoff))

    def persist_success(
        self,
        *,
        stock_id: int,
        isin: str,
        endpoint: str,
        snapshot_date: date,
        payload_hash: str,
        payload: dict,
        normalized: dict,
        next_due_at: datetime,
    ) -> int | None:
        """Atomically write raw payload, normalized rows and sync state."""
        with self._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO fundamental_raw_snapshots
                    (stock_id, source, endpoint, snapshot_date, payload_hash, status, payload)
                VALUES (%s, %s, %s, %s, %s, 'success', %s)
                ON CONFLICT (stock_id, source, endpoint, snapshot_date)
                DO UPDATE SET fetched_at = NOW(), payload_hash = EXCLUDED.payload_hash,
                              status = 'success', error_message = '', payload = EXCLUDED.payload
                RETURNING id
                """,
                (stock_id, self.SOURCE, endpoint, snapshot_date, payload_hash, Json(payload)),
            )
            raw_snapshot_id = cur.fetchone()["id"]

            if endpoint == "profile":
                self._save_profile(cur, stock_id, isin, snapshot_date, payload_hash, raw_snapshot_id, normalized)
            elif (
                endpoint.startswith(("income_statement_", "balance_sheet_", "cash_flow_"))
                or endpoint in {"balance_sheet", "cash_flow"}
            ):
                self._save_lines(cur, stock_id, endpoint, raw_snapshot_id, normalized.get("lines", []))
            elif endpoint == "key_ratios":
                self._save_ratios(cur, stock_id, snapshot_date, raw_snapshot_id, normalized.get("ratios", []))
            elif endpoint == "share_holdings":
                self._save_shareholdings(cur, stock_id, raw_snapshot_id, normalized.get("shareholdings", []))
            elif endpoint == "corporate_actions":
                self._save_actions(cur, stock_id, raw_snapshot_id, normalized.get("actions", []))
            elif endpoint == "competitors":
                self._save_competitors(cur, stock_id, snapshot_date, raw_snapshot_id, normalized.get("competitors", []))

            cur.execute(
                """
                INSERT INTO fundamental_sync_state
                    (stock_id, endpoint, last_attempted_at, last_success_at,
                     next_due_at, last_status, consecutive_failures,
                     last_payload_hash, last_error)
                VALUES (%s, %s, NOW(), NOW(), %s, 'success', 0, %s, '')
                ON CONFLICT (stock_id, endpoint)
                DO UPDATE SET last_attempted_at = NOW(), last_success_at = NOW(),
                              next_due_at = EXCLUDED.next_due_at,
                              last_status = 'success', consecutive_failures = 0,
                              last_payload_hash = EXCLUDED.last_payload_hash,
                              last_error = ''
                """,
                (stock_id, endpoint, next_due_at, payload_hash),
            )
            return raw_snapshot_id

    def persist_failure(
        self,
        *,
        stock_id: int,
        endpoint: str,
        snapshot_date: date,
        error_message: str,
        next_due_at: datetime,
    ) -> None:
        error_payload = {"error": error_message[:1000]}
        with self._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO fundamental_raw_snapshots
                    (stock_id, source, endpoint, snapshot_date, payload_hash,
                     status, error_message, payload)
                VALUES (%s, %s, %s, %s, '', 'failed', %s, %s)
                ON CONFLICT (stock_id, source, endpoint, snapshot_date)
                DO UPDATE SET fetched_at = NOW(), status = 'failed',
                              error_message = EXCLUDED.error_message,
                              payload = EXCLUDED.payload
                """,
                (stock_id, self.SOURCE, endpoint, snapshot_date, error_message[:2000], Json(error_payload)),
            )
            cur.execute(
                """
                INSERT INTO fundamental_sync_state
                    (stock_id, endpoint, last_attempted_at, next_due_at,
                     last_status, consecutive_failures, last_error)
                VALUES (%s, %s, NOW(), %s, 'failed', 1, %s)
                ON CONFLICT (stock_id, endpoint)
                DO UPDATE SET last_attempted_at = NOW(), next_due_at = EXCLUDED.next_due_at,
                              last_status = 'failed',
                              consecutive_failures = fundamental_sync_state.consecutive_failures + 1,
                              last_error = EXCLUDED.last_error
                """,
                (stock_id, endpoint, next_due_at, error_message[:2000]),
            )

    def _save_profile(self, cur, stock_id, isin, snapshot_date, payload_hash, raw_id, row):
        # The provider returns the current company market capitalisation in
        # this profile field (despite its historical ``sector_`` name). Keep
        # the application's dedicated stocks.market_cap_cr column in sync so
        # scanners can use it directly without deriving a value from P/E.
        market_cap_cr = row.get("sector_market_cap_inr")
        cur.execute(
            """
            INSERT INTO company_fundamental_profiles
                (stock_id, source, snapshot_date, isin, company_profile, sector,
                 sector_market_cap_inr, sector_market_cap_usd, payload_hash, raw_snapshot_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stock_id, source, snapshot_date)
            DO UPDATE SET isin = EXCLUDED.isin, company_profile = EXCLUDED.company_profile,
                          sector = EXCLUDED.sector,
                          sector_market_cap_inr = EXCLUDED.sector_market_cap_inr,
                          sector_market_cap_usd = EXCLUDED.sector_market_cap_usd,
                          fetched_at = NOW(), payload_hash = EXCLUDED.payload_hash,
                          raw_snapshot_id = EXCLUDED.raw_snapshot_id
            """,
            (
                stock_id, self.SOURCE, snapshot_date, isin,
                row.get("company_profile", ""), row.get("sector", ""),
                row.get("sector_market_cap_inr"), row.get("sector_market_cap_usd"),
                payload_hash, raw_id,
            ),
        )
        if market_cap_cr is not None:
            cur.execute(
                """
                UPDATE stocks
                SET market_cap_cr = %s, updated_at = NOW()
                WHERE id = %s AND %s > 0
                """,
                (market_cap_cr, stock_id, market_cap_cr),
            )

    def _save_lines(self, cur, stock_id, endpoint, raw_id, lines):
        statement_kind = next(
            kind for kind in ("income_statement", "balance_sheet", "cash_flow")
            if endpoint == kind or endpoint.startswith(f"{kind}_")
        )
        for line in lines:
            cur.execute(
                """
                INSERT INTO company_fundamental_lines
                    (stock_id, source, statement_kind, statement_type, frequency,
                     period_label, period_end, metric_name, value, change_pct, unit, raw_snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, source, statement_kind, statement_type,
                             frequency, period_label, metric_name)
                DO UPDATE SET period_end = EXCLUDED.period_end, value = EXCLUDED.value,
                              change_pct = EXCLUDED.change_pct, fetched_at = NOW(),
                              raw_snapshot_id = EXCLUDED.raw_snapshot_id
                """,
                (
                    stock_id, self.SOURCE, statement_kind, line.get("statement_type", "consolidated"),
                    line.get("frequency", "yearly"), line.get("period_label", ""),
                    line.get("period_end"), line.get("metric_name", ""), line.get("value"),
                    line.get("change_pct"), line.get("unit", "crore"), raw_id,
                ),
            )

    def _save_ratios(self, cur, stock_id, snapshot_date, raw_id, ratios):
        for ratio in ratios:
            cur.execute(
                """
                INSERT INTO company_fundamental_ratios
                    (stock_id, source, snapshot_date, ratio_name, company_value,
                     sector_value, company_raw, sector_raw, raw_snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, source, snapshot_date, ratio_name)
                DO UPDATE SET company_value = EXCLUDED.company_value,
                              sector_value = EXCLUDED.sector_value,
                              company_raw = EXCLUDED.company_raw,
                              sector_raw = EXCLUDED.sector_raw,
                              fetched_at = NOW(), raw_snapshot_id = EXCLUDED.raw_snapshot_id
                """,
                (
                    stock_id, self.SOURCE, snapshot_date, ratio.get("ratio_name", ""),
                    ratio.get("company_value"), ratio.get("sector_value"),
                    ratio.get("company_raw"), ratio.get("sector_raw"), raw_id,
                ),
            )

    def _save_shareholdings(self, cur, stock_id, raw_id, rows):
        for row in rows:
            cur.execute(
                """
                INSERT INTO company_shareholding_history
                    (stock_id, source, category, period_label, period_end, percentage, raw_snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, source, category, period_label)
                DO UPDATE SET period_end = EXCLUDED.period_end,
                              percentage = EXCLUDED.percentage, fetched_at = NOW(),
                              raw_snapshot_id = EXCLUDED.raw_snapshot_id
                """,
                (
                    stock_id, self.SOURCE, row.get("category", ""), row.get("period_label", ""),
                    row.get("period_end"), row.get("percentage"), raw_id,
                ),
            )

    def _save_actions(self, cur, stock_id, raw_id, actions):
        for action in actions:
            cur.execute(
                """
                INSERT INTO company_corporate_actions
                    (stock_id, source, event_key, action_type, effective_date,
                     announcement_date, record_date, amount, ratio, details, raw_snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, source, event_key)
                DO UPDATE SET action_type = EXCLUDED.action_type,
                              effective_date = EXCLUDED.effective_date,
                              announcement_date = EXCLUDED.announcement_date,
                              record_date = EXCLUDED.record_date,
                              amount = EXCLUDED.amount, ratio = EXCLUDED.ratio,
                              details = EXCLUDED.details, fetched_at = NOW(),
                              raw_snapshot_id = EXCLUDED.raw_snapshot_id
                """,
                (
                    stock_id, self.SOURCE, action.get("event_key", ""), action.get("action_type", ""),
                    action.get("effective_date"), action.get("announcement_date"),
                    action.get("record_date"), action.get("amount"), action.get("ratio"),
                    Json(action.get("details", {})), raw_id,
                ),
            )

    def _save_competitors(self, cur, stock_id, snapshot_date, raw_id, competitors):
        for competitor in competitors:
            cur.execute(
                """
                INSERT INTO company_competitors
                    (stock_id, source, snapshot_date, competitor_isin,
                     competitor_instrument_key, company_profile, sector,
                     sector_market_cap_inr, sector_market_cap_usd, raw_snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stock_id, source, snapshot_date, competitor_instrument_key)
                DO UPDATE SET competitor_isin = EXCLUDED.competitor_isin,
                              company_profile = EXCLUDED.company_profile,
                              sector = EXCLUDED.sector,
                              sector_market_cap_inr = EXCLUDED.sector_market_cap_inr,
                              sector_market_cap_usd = EXCLUDED.sector_market_cap_usd,
                              fetched_at = NOW(), raw_snapshot_id = EXCLUDED.raw_snapshot_id
                """,
                (
                    stock_id, self.SOURCE, snapshot_date, competitor.get("competitor_isin"),
                    competitor.get("competitor_instrument_key", ""),
                    competitor.get("company_profile", ""), competitor.get("sector", ""),
                    competitor.get("sector_market_cap_inr"), competitor.get("sector_market_cap_usd"), raw_id,
                ),
            )

    def get_stock_fundamentals(self, stock_id: int, line_limit: int = 2000) -> dict:
        """Return the latest profile/ratios plus stored historical fundamentals."""
        profile = self._db.fetch_one(
            """
            SELECT * FROM company_fundamental_profiles
            WHERE stock_id = %s
            ORDER BY snapshot_date DESC, fetched_at DESC
            LIMIT 1
            """,
            (stock_id,),
        )
        ratios = self._db.fetch_all(
            """
            WITH latest AS (
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM company_fundamental_ratios
                WHERE stock_id = %s
            )
            SELECT r.*
            FROM company_fundamental_ratios r
            JOIN latest l ON l.snapshot_date = r.snapshot_date
            WHERE r.stock_id = %s
            ORDER BY r.ratio_name
            """,
            (stock_id, stock_id),
        )
        ratio_history = self._db.fetch_all(
            """
            SELECT * FROM company_fundamental_ratios
            WHERE stock_id = %s
            ORDER BY snapshot_date DESC, ratio_name
            LIMIT %s
            """,
            (stock_id, max(1, line_limit)),
        )
        lines = self._db.fetch_all(
            """
            SELECT * FROM company_fundamental_lines
            WHERE stock_id = %s
            ORDER BY period_end DESC NULLS LAST, statement_kind, frequency, metric_name
            LIMIT %s
            """,
            (stock_id, max(1, line_limit)),
        )
        shareholdings = self._db.fetch_all(
            """
            SELECT * FROM company_shareholding_history
            WHERE stock_id = %s
            ORDER BY period_end DESC NULLS LAST, category
            """,
            (stock_id,),
        )
        actions = self._db.fetch_all(
            """
            SELECT * FROM company_corporate_actions
            WHERE stock_id = %s
            ORDER BY effective_date DESC NULLS LAST, id DESC
            """,
            (stock_id,),
        )
        competitors = self._db.fetch_all(
            """
            WITH latest AS (
                SELECT MAX(snapshot_date) AS snapshot_date
                FROM company_competitors
                WHERE stock_id = %s
            )
            SELECT c.*
            FROM company_competitors c
            JOIN latest l ON l.snapshot_date = c.snapshot_date
            WHERE c.stock_id = %s
            ORDER BY c.company_profile
            """,
            (stock_id, stock_id),
        )
        sync_state = self._db.fetch_all(
            """
            SELECT * FROM fundamental_sync_state
            WHERE stock_id = %s
            ORDER BY endpoint
            """,
            (stock_id,),
        )
        raw_latest = self._db.fetch_all(
            """
            SELECT DISTINCT ON (endpoint)
                   endpoint, snapshot_date, fetched_at, payload_hash, payload
            FROM fundamental_raw_snapshots
            WHERE stock_id = %s AND source = %s AND status = 'success'
            ORDER BY endpoint, snapshot_date DESC, fetched_at DESC
            """,
            (stock_id, self.SOURCE),
        )
        return {
            "profile": profile,
            "ratios": ratios,
            "ratio_history": ratio_history,
            "lines": lines,
            "shareholdings": shareholdings,
            "corporate_actions": actions,
            "competitors": competitors,
            "sync_state": sync_state,
            "raw_latest": raw_latest,
        }

    def get_fundamental_score_inputs(self, stock_ids: list[int]) -> dict[str, dict[str, list[dict]]]:
        """Fetch only the Upstox fields used for table scores in a few batch queries."""
        ids = sorted({int(stock_id) for stock_id in stock_ids if int(stock_id) > 0})
        result = {
            str(stock_id): {"ratios": [], "ratio_history": [], "lines": [], "shareholdings": [], "market_cap_cr": None}
            for stock_id in ids
        }
        if not ids:
            return result

        market_caps = self._db.fetch_all(
            "SELECT id AS stock_id, market_cap_cr FROM stocks WHERE id = ANY(%s)",
            (ids,),
        )
        for row in market_caps:
            target = result.get(str(row.get("stock_id")))
            if target is not None:
                target["market_cap_cr"] = row.get("market_cap_cr")

        ratios = self._db.fetch_all(
            """
            WITH latest AS (
                SELECT stock_id, MAX(snapshot_date) AS snapshot_date
                FROM company_fundamental_ratios
                WHERE source = %s AND stock_id = ANY(%s)
                GROUP BY stock_id
            )
            SELECT r.stock_id, r.ratio_name, r.company_value, r.sector_value,
                   r.company_raw, r.sector_raw
            FROM company_fundamental_ratios r
            JOIN latest l ON l.stock_id = r.stock_id AND l.snapshot_date = r.snapshot_date
            WHERE r.source = %s
            ORDER BY r.stock_id, r.ratio_name
            """,
            (self.SOURCE, ids, self.SOURCE),
        )
        lines = self._db.fetch_all(
            """
            SELECT stock_id, statement_kind, statement_type, frequency,
                   period_label, period_end, metric_name, value, change_pct, unit
            FROM company_fundamental_lines
            WHERE source = %s AND stock_id = ANY(%s)
              AND (
                (statement_kind = 'income_statement' AND frequency = 'yearly'
                  AND metric_name ILIKE ANY(%s))
                OR (statement_kind = 'balance_sheet'
                  AND metric_name IN ('total_asset', 'total_assets', 'total_liability',
                    'total_liabilities', 'total_liability_and_shareholders_funds'))
                OR (statement_kind = 'cash_flow' AND metric_name ILIKE %s)
              )
            ORDER BY stock_id, period_end DESC NULLS LAST, metric_name
            """,
            (self.SOURCE, ids, ["%revenue%", "%sales%", "%income%", "%profit%", "%pat%"], "%operat%"),
        )
        shareholdings = self._db.fetch_all(
            """
            SELECT stock_id, category, period_label, period_end, percentage
            FROM company_shareholding_history
            WHERE source = %s AND stock_id = ANY(%s) AND category ILIKE %s
            ORDER BY stock_id, period_end DESC NULLS LAST, category
            """,
            (self.SOURCE, ids, "%promoter%"),
        )

        for rows, key in ((ratios, "ratios"), (lines, "lines"), (shareholdings, "shareholdings")):
            for row in rows:
                stock_id = str(row.get("stock_id"))
                target = result.get(stock_id)
                if target is None:
                    continue
                item = {field: value for field, value in row.items() if field != "stock_id"}
                target[key].append(item)
        return result

    def get_sync_status(self, limit: int = 20) -> list[dict]:
        """Return recent fundamentals runs for operational monitoring."""
        return self._db.fetch_all(
            """
            SELECT * FROM fundamental_sync_runs
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (max(1, limit),),
        )

    def get_sync_coverage(self, endpoints: list[str]) -> list[dict]:
        """Summarize mapped, fresh, due and failed stocks per endpoint."""
        return self._db.fetch_all(
            f"""
            WITH endpoint_names AS (
                SELECT unnest(%s::text[]) AS endpoint
            ), universe AS (
                SELECT id, isin
                FROM stocks
                WHERE {equity_universe_sql('stocks')}
            )
            SELECT e.endpoint,
                   COUNT(u.id) AS total_stocks,
                   COUNT(u.id) FILTER (WHERE COALESCE(u.isin, '') <> '') AS mapped_stocks,
                   COUNT(u.id) FILTER (
                       WHERE COALESCE(u.isin, '') <> ''
                         AND fs.last_success_at IS NOT NULL
                         AND fs.next_due_at > NOW()
                   ) AS fresh_stocks,
                   COUNT(u.id) FILTER (
                       WHERE COALESCE(u.isin, '') <> ''
                         AND (fs.last_success_at IS NULL OR fs.next_due_at IS NULL OR fs.next_due_at <= NOW())
                   ) AS due_stocks,
                   COUNT(u.id) FILTER (WHERE fs.last_status = 'failed') AS failed_stocks,
                   MAX(fs.last_success_at) AS last_success_at
            FROM endpoint_names e
            CROSS JOIN universe u
            LEFT JOIN fundamental_sync_state fs
              ON fs.stock_id = u.id AND fs.endpoint = e.endpoint
            GROUP BY e.endpoint
            ORDER BY e.endpoint
            """,
            (list(endpoints),),
        )
