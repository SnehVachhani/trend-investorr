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

from datetime import datetime, date
from typing import Any

from loguru import logger

from storage.database import Database


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
                industry = EXCLUDED.industry,
                lot_size = EXCLUDED.lot_size,
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
                industry = EXCLUDED.industry, lot_size = EXCLUDED.lot_size,
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

    def get_by_sector(self, sector_id: int) -> list[dict]:
        return self._db.fetch_all(
            "SELECT * FROM stocks WHERE sector_id = %s AND is_active = TRUE ORDER BY symbol",
            (sector_id,),
        )

    def get_all_active(self, exchange: str = "NSE") -> list[dict]:
        return self._db.fetch_all(
            "SELECT * FROM stocks WHERE exchange = %s AND is_active = TRUE ORDER BY symbol",
            (exchange,),
        )

    def update_sector(self, stock_id: int, sector_id: int) -> None:
        self._db.execute(
            "UPDATE stocks SET sector_id = %s, updated_at = NOW() WHERE id = %s",
            (sector_id, stock_id),
        )


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
            """
            SELECT
                wl.id, wl.name, wl.description, wl.list_type,
                wl.max_stocks, wl.is_active,
                COUNT(DISTINCT wi.stock_id) AS stock_count,
                COALESCE(
                    ARRAY_AGG(DISTINCT wab.algorithm_name)
                    FILTER (WHERE wab.algorithm_name IS NOT NULL AND wab.is_active = TRUE),
                    ARRAY[]::VARCHAR[]
                ) AS bound_algorithms
            FROM watchlists wl
            LEFT JOIN watchlist_items wi ON wi.watchlist_id = wl.id
            LEFT JOIN watchlist_algorithm_bindings wab ON wab.watchlist_id = wl.id
            WHERE wl.is_active = TRUE
            GROUP BY wl.id
            ORDER BY wl.name
            """
        )

    # ── Watchlist Items (child) ──

    def add_stock(self, watchlist_name: str, stock_id: int, source: str = "manual", notes: str = "") -> bool:
        """Add a stock to a watchlist. Returns True if added, False if already exists."""
        wl = self.get_watchlist(watchlist_name)
        if not wl:
            return False

        # Check max_stocks limit (0 = no limit)
        if wl["max_stocks"] > 0:
            count = self._db.fetch_one(
                "SELECT COUNT(*) as cnt FROM watchlist_items WHERE watchlist_id = %s",
                (wl["id"],),
            )
            if count and count["cnt"] >= wl["max_stocks"]:
                return False

        self._db.execute(
            """
            INSERT INTO watchlist_items (watchlist_id, stock_id, source, notes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (watchlist_id, stock_id) DO NOTHING
            """,
            (wl["id"], stock_id, source, notes),
        )
        return True

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
            """
            SELECT wi.*, s.symbol, s.token, s.name, s.exchange
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            JOIN watchlists wl ON wl.id = wi.watchlist_id
            WHERE wl.name = %s AND s.is_active = TRUE
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
            """
            SELECT DISTINCT s.token
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            JOIN watchlists wl ON wl.id = wi.watchlist_id
            WHERE wl.is_active = TRUE AND s.is_active = TRUE
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
            """
            SELECT stock_id, AVG(volume) as avg_vol
            FROM candles_1d
            WHERE timestamp > NOW() - INTERVAL '30 days'
            GROUP BY stock_id
            HAVING AVG(volume) > 0
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
        Build the token → set[algorithm_name] routing table.

        Joins watchlist_algorithm_bindings → watchlist_items → stocks
        to produce: { "2885": {"scanner_confluence", "connors_rsi"}, ... }

        This is called once at scanner_warmup() and cached in AlgorithmManager.
        Deduplicates automatically — if the same stock appears in multiple
        watchlists bound to the same algorithm, it appears once.

        Returns:
            Empty dict if no bindings exist (triggers fallback behavior).
        """
        rows = self._db.fetch_all(
            """
            SELECT DISTINCT s.token, wab.algorithm_name
            FROM watchlist_algorithm_bindings wab
            JOIN watchlists wl ON wl.id = wab.watchlist_id
            JOIN watchlist_items wi ON wi.watchlist_id = wl.id
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wab.is_active = TRUE
              AND wl.is_active = TRUE
              AND s.is_active = TRUE
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
        Get ALL unique tokens from ALL active watchlists for WebSocket subscription.

        This is the union of all watchlist items across all active watchlists,
        regardless of algorithm bindings. We subscribe to everything —
        filtering happens at the observation routing layer.

        Returns:
            List of unique SmartAPI tokens. Falls back to empty list
            if no watchlists are configured.
        """
        rows = self._db.fetch_all(
            """
            SELECT DISTINCT s.token
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            JOIN watchlists wl ON wl.id = wi.watchlist_id
            WHERE wl.is_active = TRUE AND s.is_active = TRUE
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
            """
            SELECT
                wl.name AS watchlist_name,
                wab.algorithm_name,
                COUNT(DISTINCT wi.stock_id) AS stock_count
            FROM watchlist_algorithm_bindings wab
            JOIN watchlists wl ON wl.id = wab.watchlist_id
            LEFT JOIN watchlist_items wi ON wi.watchlist_id = wl.id
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

    # ── Reads ────────────────────────────────────────────

    def get_candles(self, stock_id: int, interval, days: int = 90) -> list[dict]:
        """Fetch candles for a stock, most recent N days, sorted timestamp DESC."""
        table = self._table(interval)
        return self._db.fetch_all(
            f"""
            SELECT * FROM {table}
            WHERE stock_id = %s
              AND timestamp >= NOW() - INTERVAL '%s days'
            ORDER BY timestamp DESC
            """,
            (stock_id, days),
        )

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
            """
            SELECT s.token, COALESCE(AVG(c.volume), 0)::BIGINT AS avg_volume
            FROM stocks s
            JOIN LATERAL (
                SELECT volume FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT %s
            ) c ON TRUE
            WHERE s.is_active = TRUE
            GROUP BY s.token
            """,
            (days_30,),
        )
        avg_30 = {row["token"]: int(row["avg_volume"]) for row in rows_30}

        rows_90 = self._db.fetch_all(
            """
            SELECT s.token, COALESCE(AVG(c.volume), 0)::BIGINT AS avg_volume
            FROM stocks s
            JOIN LATERAL (
                SELECT volume FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT %s
            ) c ON TRUE
            WHERE s.is_active = TRUE
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
    ) -> None:
        """Insert or update coverage for a stock + interval."""
        self._db.execute(
            """
            INSERT INTO candle_coverage
                (stock_id, interval, earliest_ts, latest_ts, candle_count, source, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (stock_id, interval) DO UPDATE SET
                earliest_ts  = LEAST(candle_coverage.earliest_ts, EXCLUDED.earliest_ts),
                latest_ts    = GREATEST(candle_coverage.latest_ts, EXCLUDED.latest_ts),
                candle_count = candle_coverage.candle_count + EXCLUDED.candle_count,
                source       = EXCLUDED.source,
                last_updated = NOW()
            """,
            (stock_id, interval, earliest, latest, candle_count, source),
        )

    def delete_coverage(self, stock_id: int, interval: str) -> None:
        """Delete coverage record."""
        self._db.execute(
            "DELETE FROM candle_coverage WHERE stock_id = %s AND interval = %s",
            (stock_id, interval),
        )

    def mark_empty_day(self, stock_id: int, interval: str, empty_date: date) -> None:
        """Mark a specific day as empty for a stock so it is not repeatedly requested."""
        query = """
            INSERT INTO empty_stock_days (stock_id, interval, date)
            VALUES (%s, %s, %s)
            ON CONFLICT (stock_id, interval, date) DO NOTHING
        """
        self._db.execute(query, (stock_id, interval, empty_date))

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

    def refresh_candle_counts(self, interval: str) -> int:
        """
        Recompute candle_count for ALL stocks from actual candle table data.

        Fixes count drift caused by the upsert() additive accumulation.
        Should be called after batch operations (backfill, hole-fill, post-market).

        Returns number of rows updated.
        """
        from core.constants import CandleInterval, CANDLE_TABLE_SUFFIX
        iv_enum = CandleInterval(interval) if isinstance(interval, str) else interval
        suffix = CANDLE_TABLE_SUFFIX.get(iv_enum)
        if not suffix:
            raise ValueError(f"Unknown interval: {interval}")
        table = f"candles_{suffix}"

        result = self._db.execute(
            f"""
            UPDATE candle_coverage cc SET
                candle_count = sub.real_count,
                last_updated = NOW()
            FROM (
                SELECT stock_id, COUNT(*) AS real_count
                FROM {table}
                GROUP BY stock_id
            ) sub
            WHERE cc.stock_id = sub.stock_id AND cc.interval = %s
            """,
            (interval,),
        )
        logger.info(f"Coverage counts refreshed for {interval}: {result} stocks reconciled")
        return result

    def get_trading_days(self, start_date: date, end_date: date) -> list[date]:
        """
        Get the list of expected NSE trading days between start_date and end_date.
        Uses the holiday calendar from core.constants.
        """
        from core.constants import get_nse_trading_days
        return get_nse_trading_days(start_date, end_date)

    def find_all_holes(
        self,
        interval: str,
        exchange: str = "NSE",
        default_missing_days: int = 90,
    ) -> list[dict]:
        """
        For EVERY active stock, find which trading days are missing candles.
        If a stock has NO data, it assumes `default_missing_days` are missing.

        Uses the NSE holiday calendar to determine expected trading days.
        Only checks within each stock's own coverage envelope
        (earliest_ts to latest_ts) — or synthetic envelope for new stocks.

        Returns:
            List of dicts, one per stock WITH holes:
            [
                {"stock_id": 1, "symbol": "RELIANCE", "token": "2885",
                 "missing_dates": [date(2026,2,15), date(2026,2,16), ...]},
                ...
            ]
        """
        from datetime import datetime, timedelta
        from core.constants import CandleInterval, CANDLE_TABLE_SUFFIX, IST

        iv_enum = CandleInterval(interval) if isinstance(interval, str) else interval
        suffix = CANDLE_TABLE_SUFFIX.get(iv_enum)
        if not suffix:
            raise ValueError(f"Unknown interval: {interval}")
        table = f"candles_{suffix}"

        # 1. Get all stocks with coverage for this interval
        coverage_rows = self._db.fetch_all(
            """
            SELECT cc.stock_id, cc.earliest_ts, cc.latest_ts,
                   s.symbol, s.token
            FROM candle_coverage cc
            JOIN stocks s ON s.id = cc.stock_id
            WHERE cc.interval = %s
              AND s.is_active = TRUE
              AND s.exchange = %s
              AND cc.earliest_ts IS NOT NULL
              AND cc.latest_ts IS NOT NULL
            ORDER BY s.symbol
            """,
            (interval, exchange),
        )

        # 1b. Get totally missing stocks (no coverage at all)
        missing_stocks = self._db.fetch_all(
            """
            SELECT s.id AS stock_id, s.symbol, s.token
            FROM stocks s
            WHERE s.is_active = TRUE
              AND s.exchange = %s
              AND NOT EXISTS (
                  SELECT 1 FROM candle_coverage cc 
                  WHERE cc.stock_id = s.id AND cc.interval = %s
              )
            """,
            (exchange, interval),
        )

        # Inject synthetic coverage for completely missing stocks
        now = datetime.now(tz=IST)
        synthetic_start = now - timedelta(days=default_missing_days)
        
        for ms in missing_stocks:
            coverage_rows.append({
                "stock_id": ms["stock_id"],
                "symbol": ms["symbol"],
                "token": ms["token"],
                "earliest_ts": synthetic_start,
                "latest_ts": now,
            })

        if not coverage_rows:
            return []

        # 2. Get actual candle dates for ALL active stocks in one query
        actual_dates_raw = self._db.fetch_all(
            f"""
            SELECT stock_id, timestamp::DATE AS candle_date
            FROM {table}
            WHERE stock_id IN (
                SELECT id FROM stocks WHERE is_active = TRUE AND exchange = %s
            )
            GROUP BY stock_id, candle_date
            ORDER BY stock_id, candle_date
            """,
            (exchange,),
        )

        # Build {stock_id: set(dates)}
        actual_dates_by_stock: dict[int, set[date]] = {}
        for row in actual_dates_raw:
            sid = row["stock_id"]
            if sid not in actual_dates_by_stock:
                actual_dates_by_stock[sid] = set()
            actual_dates_by_stock[sid].add(row["candle_date"])

        # 2b. Get empty days for ALL active stocks in one query
        empty_dates_raw = self._db.fetch_all(
            f"""
            SELECT stock_id, date AS empty_date
            FROM empty_stock_days
            WHERE interval = %s AND stock_id IN (
                SELECT id FROM stocks WHERE is_active = TRUE AND exchange = %s
            )
            """,
            (interval, exchange),
        )

        for row in empty_dates_raw:
            sid = row["stock_id"]
            if sid not in actual_dates_by_stock:
                actual_dates_by_stock[sid] = set()
            actual_dates_by_stock[sid].add(row["empty_date"])

        # 3. For each stock, compare expected vs actual within coverage range
        results = []
        for cov in coverage_rows:
            sid = cov["stock_id"]
            cov_start = cov["earliest_ts"].date() if hasattr(cov["earliest_ts"], "date") else cov["earliest_ts"]
            cov_end = cov["latest_ts"].date() if hasattr(cov["latest_ts"], "date") else cov["latest_ts"]

            expected = self.get_trading_days(cov_start, cov_end)
            actual = actual_dates_by_stock.get(sid, set())
            missing = [d for d in expected if d not in actual]

            if missing:
                results.append({
                    "stock_id": sid,
                    "symbol": cov["symbol"],
                    "token": cov["token"],
                    "missing_dates": sorted(missing),
                })

        logger.info(
            f"Hole detection for {interval}: "
            f"{len(coverage_rows)} stocks checked "
            f"({len(missing_stocks)} completely un-ingested), "
            f"{len(results)} with holes, "
            f"{sum(len(r['missing_dates']) for r in results)} total missing stock-days"
        )
        return results


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
                "SELECT id FROM stocks WHERE symbol = %s AND exchange = 'NSE' LIMIT 1",
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
            """
            SELECT ss.id, ss.scanner_name, ss.pattern, ss.direction,
                   ss.confidence, ss.price, ss.strength, ss.details,
                   s.symbol, s.token
            FROM scanner_signals ss
            JOIN stocks s ON s.id = ss.stock_id
            WHERE ss.target_date = %s AND ss.status = 'ACTIVE'
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

