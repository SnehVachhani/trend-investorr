"""
Trend-Investorr: Candle Aggregator

Real-time aggregation of raw ticks into OHLCV candles at configurable
intervals (default: 5 minutes).

How it works:
    1. Each incoming tick updates the current "bucket" for that stock
    2. A bucket tracks: first_tick_time, open, high, low, close, volume
    3. When the clock crosses a 5-minute boundary (e.g., 10:20 → 10:25),
       the completed bucket is finalized and queued for DB flush
    4. A background thread flushes completed candles to candles_5m table
    5. At market close (3:30 PM), all remaining partial buckets are flushed

Boundary Logic:
    Candle timestamps are floored to 5-minute boundaries:
        tick at 10:22:45 → belongs to candle starting at 10:20:00
        tick at 10:25:01 → belongs to candle starting at 10:25:00
                           (this also finalizes the 10:20 candle)
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from core.constants import CandleInterval, IST
from data.models.market_data import Tick


# Default candle interval in minutes
CANDLE_INTERVAL_MIN = 5

# Flush completed candles to DB every N seconds
FLUSH_INTERVAL_SEC = 10.0


@dataclass
class CandleBucket:
    """
    Accumulates ticks for a single stock within one candle interval.

    A bucket is "open" while the current time is within its interval
    and "completed" once the next interval begins.
    """

    token: str
    symbol: str
    interval_start: datetime

    # OHLCV — updated on every tick
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: int = 0
    tick_count: int = 0

    # VWAP tracking
    _cum_tp_vol: float = field(default=0.0, repr=False)
    _cum_vol: int = field(default=0, repr=False)

    def update(self, tick: Tick) -> None:
        """Update the bucket with a new tick."""
        if self.tick_count == 0:
            self.open = tick.ltp

        self.high = max(self.high, tick.ltp)
        if tick.ltp > 0:
            self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        self.volume = tick.volume  # WebSocket sends cumulative volume
        self.tick_count += 1

        # VWAP approximation
        if tick.ltp > 0:
            self._cum_tp_vol += tick.ltp * (tick.volume if tick.volume > 0 else 1)
            self._cum_vol += tick.volume if tick.volume > 0 else 1

    @property
    def vwap(self) -> float:
        """Volume-weighted average price for this candle."""
        if self._cum_vol == 0:
            return self.close
        return self._cum_tp_vol / self._cum_vol

    @property
    def is_valid(self) -> bool:
        """A bucket is valid if it has received at least one tick."""
        return self.tick_count > 0

    def to_tuple(self, stock_id: int | None) -> tuple:
        """
        Convert to a tuple for database insertion into candles_Xm tables.

        Returns:
            (stock_id, timestamp, open, high, low, close, volume, vwap)
        """
        return (
            stock_id,
            self.interval_start,
            self.open,
            self.high,
            self.low if self.low != float("inf") else self.close,
            self.close,
            self.volume,
            round(self.vwap, 2),
        )


class CandleAggregator:
    """
    Real-time tick → candle aggregator.

    Maintains per-stock candle buckets in memory and flushes completed
    candles to the database periodically.

    Usage:
        agg = CandleAggregator(db=database, interval_min=5)
        agg.start()

        # In WebSocket callback:
        agg.on_tick(tick)

        # At market close:
        agg.flush_all()
        agg.stop()
    """

    def __init__(
        self,
        db: Any,
        interval_min: int = CANDLE_INTERVAL_MIN,
        token_to_stock_id: dict[str, int] | None = None,
    ):
        """
        Args:
            db: Database instance.
            interval_min: Candle interval in minutes (default: 5).
            token_to_stock_id: Token → stock_id mapping for DB foreign keys.
        """
        self._db = db
        self._interval_min = interval_min
        self._interval_delta = timedelta(minutes=interval_min)
        self._token_to_stock_id = token_to_stock_id or {}

        # Map interval_min → CandleInterval enum for CandleStore
        _interval_map = {
            1: CandleInterval.ONE_MINUTE,
            5: CandleInterval.FIVE_MINUTE,
            15: CandleInterval.FIFTEEN_MINUTE,
            60: CandleInterval.ONE_HOUR,
        }
        self._candle_interval = _interval_map.get(interval_min, CandleInterval.FIVE_MINUTE)
        self._interval_label = self._candle_interval.value

        # Active buckets: token → CandleBucket
        self._buckets: dict[str, CandleBucket] = {}
        self._lock = threading.Lock()

        # Completed candles waiting to be flushed
        self._completed: list[CandleBucket] = []
        self._completed_lock = threading.Lock()

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        self._running = False

        # Stats
        self._candles_flushed: int = 0
        self._ticks_processed: int = 0

    def start(self) -> None:
        """Start the background candle flush thread."""
        if self._running:
            return

        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name="CandleAggregator-Flush",
            daemon=True,
        )
        self._flush_thread.start()
        logger.info(
            f"CandleAggregator started: interval={self._interval_label}, "
            f"flush_interval={FLUSH_INTERVAL_SEC}s"
        )

    def stop(self) -> None:
        """Stop the background thread and flush all remaining candles."""
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)

        # Final flush
        self.flush_all()
        logger.info(
            f"CandleAggregator stopped | candles_flushed={self._candles_flushed}, "
            f"ticks_processed={self._ticks_processed:,}"
        )

    def on_tick(self, tick: Tick) -> None:
        """
        Process a single tick — update or rotate the candle bucket.

        Thread-safe. Called from the WebSocket callback thread.

        Args:
            tick: Real-time tick from WebSocket.
        """
        self._ticks_processed += 1

        # Determine which candle interval this tick belongs to
        bucket_start = self._floor_to_interval(tick.timestamp)

        with self._lock:
            existing = self._buckets.get(tick.token)

            if existing is None:
                # First tick for this stock — create new bucket
                bucket = CandleBucket(
                    token=tick.token,
                    symbol=tick.symbol,
                    interval_start=bucket_start,
                )
                bucket.update(tick)
                self._buckets[tick.token] = bucket

            elif existing.interval_start != bucket_start:
                # New interval! Finalize the old bucket and start a new one
                if existing.is_valid:
                    with self._completed_lock:
                        self._completed.append(existing)

                bucket = CandleBucket(
                    token=tick.token,
                    symbol=tick.symbol,
                    interval_start=bucket_start,
                )
                bucket.update(tick)
                self._buckets[tick.token] = bucket

            else:
                # Same interval — just update
                existing.update(tick)

    def flush_all(self) -> int:
        """
        Flush ALL buckets (active + completed) to the database.

        Called at market close to ensure no data is lost.

        Returns:
            Number of candles flushed.
        """
        # Move all active buckets to completed
        with self._lock:
            for token, bucket in self._buckets.items():
                if bucket.is_valid:
                    with self._completed_lock:
                        self._completed.append(bucket)
            self._buckets.clear()

        return self._do_flush()

    def set_token_mapping(self, mapping: dict[str, int]) -> None:
        """Update the token → stock_id mapping."""
        self._token_to_stock_id = mapping
        logger.debug(f"CandleAggregator token mapping updated: {len(mapping)} tokens")

    def reset_daily(self) -> None:
        """Reset all buckets for a new trading day."""
        with self._lock:
            self._buckets.clear()
        with self._completed_lock:
            self._completed.clear()
        logger.info("CandleAggregator reset for new trading day")

    # ── Internal ─────────────────────────────────────────

    def _floor_to_interval(self, dt: datetime) -> datetime:
        """
        Floor a datetime to the nearest candle interval boundary.

        Example (5-min): 10:22:45 → 10:20:00
        """
        minute = (dt.minute // self._interval_min) * self._interval_min
        return dt.replace(minute=minute, second=0, microsecond=0)

    def _flush_loop(self) -> None:
        """Background thread: flush completed candles periodically."""
        while self._running:
            time.sleep(FLUSH_INTERVAL_SEC)
            if self._completed:
                self._do_flush()

    def _do_flush(self) -> int:
        """Batch-insert completed candles into the database."""
        with self._completed_lock:
            if not self._completed:
                return 0
            batch = list(self._completed)
            self._completed.clear()

        # Convert to tuples for DB insertion
        rows = []
        for bucket in batch:
            stock_id = self._token_to_stock_id.get(bucket.token)
            rows.append(bucket.to_tuple(stock_id))

        if not rows:
            return 0

        try:
            from storage.models import CandleStore
            store = CandleStore(self._db)
            store.insert_batch(self._candle_interval, rows)

            self._candles_flushed += len(rows)

            if self._candles_flushed % 500 == 0:
                logger.debug(
                    f"CandleAggregator: flushed {len(rows)} candles "
                    f"(total={self._candles_flushed:,})"
                )

            return len(rows)

        except Exception as e:
            logger.error(f"CandleAggregator flush error: {e} | lost {len(rows)} candles")
            return 0

    # ── Accessors ────────────────────────────────────────

    @property
    def active_buckets(self) -> int:
        """Number of stocks with active (open) candle buckets."""
        return len(self._buckets)

    @property
    def pending_candles(self) -> int:
        """Number of completed candles waiting to be flushed."""
        return len(self._completed)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "active_buckets": len(self._buckets),
            "pending_candles": len(self._completed),
            "candles_flushed": self._candles_flushed,
            "ticks_processed": self._ticks_processed,
            "interval": self._interval_label,
            "is_running": self._running,
        }

    def __repr__(self) -> str:
        return (
            f"CandleAggregator(interval={self._interval_label}, "
            f"active={len(self._buckets)}, "
            f"flushed={self._candles_flushed:,})"
        )
