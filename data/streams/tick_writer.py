"""
Trend-Investorr: Tick Writer

Batched database writer for live tick data. Buffers ticks in memory
and flushes them to the `live_ticks` table in bulk to avoid overwhelming
PostgreSQL with thousands of individual INSERT statements per second.

Design:
    - Collects ticks into a thread-safe in-memory buffer
    - Flushes to DB when buffer reaches BATCH_SIZE (500) or
      FLUSH_INTERVAL_SEC (2 seconds), whichever comes first
    - Flush runs in a background thread to avoid blocking WebSocket
    - Auto-purges ticks older than RETENTION_DAYS (3) on daily reset

Data Volume Estimate:
    500 stocks × 1 tick/sec avg = ~500 ticks/sec
    Buffer of 500 flushes ~1x/sec
    ~11M rows/day → purged after 3 days = ~33M max rows
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST
from data.models.market_data import Tick


# Batching parameters
BATCH_SIZE = 500           # Flush after this many ticks buffered
FLUSH_INTERVAL_SEC = 2.0   # Flush at least every N seconds
RETENTION_DAYS = 3         # Auto-purge ticks older than N days


class TickWriter:
    """
    Batched tick writer for PostgreSQL.

    Buffers incoming ticks and flushes them to the live_ticks table
    in efficient bulk INSERTs. Thread-safe for use from WebSocket callbacks.

    Usage:
        writer = TickWriter(db=database)
        writer.start()

        # In WebSocket callback (called thousands of times/sec):
        writer.buffer(tick)

        # At market close:
        writer.flush()
        writer.stop()

        # Daily maintenance:
        writer.purge_old_ticks()
    """

    def __init__(self, db: Any, token_to_stock_id: dict[str, int] | None = None):
        """
        Args:
            db: Database instance for executing queries.
            token_to_stock_id: Optional mapping of token -> stock_id for
                               foreign key linkage. If None, stock_id is
                               set to None in the database.
        """
        self._db = db
        self._token_to_stock_id = token_to_stock_id or {}

        # Thread-safe buffer
        self._buffer: deque[tuple] = deque()
        self._lock = threading.Lock()

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        self._running = False

        # Stats
        self._total_buffered: int = 0
        self._total_flushed: int = 0
        self._total_flushes: int = 0
        self._flush_errors: int = 0

    def start(self) -> None:
        """Start the background flush thread."""
        if self._running:
            return

        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name="TickWriter-Flush",
            daemon=True,
        )
        self._flush_thread.start()
        logger.info("TickWriter started (background flush thread active)")

    def stop(self) -> None:
        """Stop the background flush thread and flush remaining buffer."""
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)

        # Final flush of remaining ticks
        self._do_flush()
        logger.info(
            f"TickWriter stopped | total_buffered={self._total_buffered:,} "
            f"total_flushed={self._total_flushed:,} flushes={self._total_flushes}"
        )

    def buffer(self, tick: Tick) -> None:
        """
        Add a tick to the write buffer.

        Thread-safe. Called from the WebSocket callback thread.
        The tick is formatted into a tuple ready for batch INSERT.

        Args:
            tick: Real-time tick from WebSocket.
        """
        stock_id = self._token_to_stock_id.get(tick.token)

        row = (
            stock_id,           # stock_id (nullable FK)
            tick.token,         # token
            tick.timestamp,     # timestamp
            tick.ltp,           # ltp
            tick.volume,        # volume
            tick.open,          # open
            tick.high,          # high
            tick.low,           # low
            tick.best_bid,      # best_bid
            tick.best_ask,      # best_ask
            tick.avg_trade_price,  # avg_trade_price
        )

        with self._lock:
            self._buffer.append(row)
            self._total_buffered += 1

    def flush(self) -> int:
        """
        Manually trigger a flush of the current buffer.

        Returns:
            Number of ticks flushed.
        """
        return self._do_flush()

    def set_token_mapping(self, mapping: dict[str, int]) -> None:
        """Update the token -> stock_id mapping (e.g., after instrument sync)."""
        self._token_to_stock_id = mapping
        logger.debug(f"TickWriter token mapping updated: {len(mapping)} tokens")

    def purge_old_ticks(self, days: int = RETENTION_DAYS) -> int:
        """
        Delete ticks older than N days.

        Called during pre-market daily reset.

        Returns:
            Number of rows deleted.
        """
        try:
            from storage.models import TickStore
            store = TickStore(self._db)
            count = store.purge_older_than(days)
            return count
        except Exception as e:
            logger.error(f"Tick purge failed: {e}")
            return 0

    # ── Internal ─────────────────────────────────────────

    def _flush_loop(self) -> None:
        """Background thread: flush buffer periodically."""
        while self._running:
            time.sleep(FLUSH_INTERVAL_SEC)

            # Flush if buffer has any data
            if len(self._buffer) > 0:
                self._do_flush()

    def _do_flush(self) -> int:
        """Drain the buffer and batch-insert into the database."""
        # Grab all buffered ticks atomically
        with self._lock:
            if not self._buffer:
                return 0

            batch = list(self._buffer)
            self._buffer.clear()

        # Batch insert
        try:
            from storage.models import TickStore
            store = TickStore(self._db)
            store.insert_batch(batch)

            self._total_flushed += len(batch)
            self._total_flushes += 1

            if self._total_flushes % 100 == 0:
                logger.debug(
                    f"TickWriter flush #{self._total_flushes}: "
                    f"{len(batch)} ticks | total={self._total_flushed:,}"
                )

            return len(batch)

        except Exception as e:
            self._flush_errors += 1
            logger.error(
                f"TickWriter flush error (attempt #{self._flush_errors}): {e} | "
                f"lost {len(batch)} ticks"
            )
            return 0

    # ── Accessors ────────────────────────────────────────

    @property
    def buffer_size(self) -> int:
        """Current number of ticks waiting to be flushed."""
        return len(self._buffer)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "buffer_size": len(self._buffer),
            "total_buffered": self._total_buffered,
            "total_flushed": self._total_flushed,
            "total_flushes": self._total_flushes,
            "flush_errors": self._flush_errors,
            "is_running": self._running,
        }

    def __repr__(self) -> str:
        return (
            f"TickWriter(buffer={len(self._buffer)}, "
            f"flushed={self._total_flushed:,}, "
            f"running={self._running})"
        )
