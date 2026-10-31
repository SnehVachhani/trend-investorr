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
        tick at 10:25:00 → belongs to candle starting at 10:25:00
                           (this also finalizes the 10:20 candle)

Volume Logic:
    SmartAPI's ``volume_trade_for_the_day`` (→ ``Tick.volume``) is the
    *cumulative* volume traded so far today, not the size of one trade.
    The aggregator therefore remembers the previous cumulative reading per
    token and adds only the increase to the current candle; that increase
    is also the VWAP weight for the tick.

    First reading for a token:
        - inside the 09:15 opening interval → the whole reading is volume
          traded in the opening candle (the day's counter started at 0).
        - later in the session (late subscribe / reconnect) → the reading is
          a baseline with zero new volume. A candle that began with that
          baseline and has observed no new volume since is never saved.

    A reading lower than the last reliable counter (stale / out-of-order) adds
    nothing and does not replace the counter, so the next higher reading is
    still measured against the reliable value. reset_daily() clears the
    counters for a genuine new trading day.

Durability:
    A candle leaves the pending queue only once it has been written. On
    stop(), anything still pending (failed DB write, token without a
    stock_id) is spilled to ``spill_path`` as JSON. The next start() reloads
    it into the pending queue but keeps the file: while it exists it is
    rewritten after every successful flush to mirror what is still pending,
    and deleted only once nothing is pending. A process that recovers the
    candles and dies before writing them therefore loses nothing. (The DB
    upsert on (stock_id, timestamp) makes a re-write after a crash between
    the DB commit and the file rewrite harmless.)

    A spill file that cannot be loaded completely (unreadable, wrong
    interval, any corrupt entry) is *blocked*: nothing from it is loaded, it
    is never deleted or overwritten, and it is reported in stats / at stop()
    until an operator fixes or moves it. While the main file is blocked, new
    candles are spilled to a sibling ``<name>.unsaved-<timestamp>`` file,
    which recovery scans as well.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

from loguru import logger

from core.constants import MARKET_OPEN, CandleInterval, IST
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

    # True while the bucket started with a mid-session baseline reading
    # (a token's first tick after 09:15) and has not yet observed any new
    # volume. Such buckets are never persisted.
    _baseline_only: bool = field(default=False, repr=False)

    def update(self, tick: Tick, volume_delta: int, is_baseline: bool = False) -> None:
        """
        Update the bucket with a new tick.

        Args:
            tick: The tick (``tick.volume`` is the day's cumulative volume).
            volume_delta: Volume traded since the previous reading for this
                token — the only part that belongs to this candle.
            is_baseline: True when this is a mid-session first reading whose
                volume is unknown and must not be counted.
        """
        if self.tick_count == 0:
            self.open = tick.ltp
            self._baseline_only = is_baseline

        self.high = max(self.high, tick.ltp)
        if tick.ltp > 0:
            self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        self.tick_count += 1

        if volume_delta <= 0:
            return

        # Real volume observed → this is a persistable candle
        self._baseline_only = False
        self.volume += volume_delta
        if tick.ltp > 0:
            self._cum_tp_vol += tick.ltp * volume_delta
            self._cum_vol += volume_delta

    @property
    def vwap(self) -> float:
        """Volume-weighted average price for this candle."""
        if self._cum_vol == 0:
            return self.close
        return self._cum_tp_vol / self._cum_vol

    @property
    def is_baseline_only(self) -> bool:
        """True if the bucket began with a mid-session baseline reading and
        has observed no new volume since."""
        return self._baseline_only

    @property
    def is_valid(self) -> bool:
        """A bucket is valid (persistable) if it has at least one tick and is
        not merely a baseline observation with no observed volume."""
        return self.tick_count > 0 and not self._baseline_only

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full bucket state (for the shutdown spill file)."""
        return {
            "token": self.token,
            "symbol": self.symbol,
            "interval_start": self.interval_start.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low if self.low != float("inf") else None,
            "close": self.close,
            "volume": self.volume,
            "tick_count": self.tick_count,
            "cum_tp_vol": self._cum_tp_vol,
            "cum_vol": self._cum_vol,
            "baseline_only": self._baseline_only,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandleBucket":
        """Inverse of to_dict()."""
        bucket = cls(
            token=str(data["token"]),
            symbol=str(data.get("symbol", "")),
            interval_start=datetime.fromisoformat(data["interval_start"]).astimezone(IST),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]) if data.get("low") is not None else float("inf"),
            close=float(data["close"]),
            volume=int(data["volume"]),
            tick_count=int(data["tick_count"]),
        )
        bucket._cum_tp_vol = float(data.get("cum_tp_vol", 0.0))
        bucket._cum_vol = int(data.get("cum_vol", 0))
        bucket._baseline_only = bool(data.get("baseline_only", False))
        return bucket

    @property
    def key(self) -> tuple[str, datetime]:
        """Identity of the candle: (token, interval_start)."""
        return (self.token, self.interval_start)

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
        spill_path: str | Path | None = None,
    ):
        """
        Args:
            db: Database instance.
            interval_min: Candle interval in minutes (default: 5).
            token_to_stock_id: Token → stock_id mapping for DB foreign keys.
            spill_path: File where candles still pending at stop() are saved
                so they survive a restart. None disables persistence (they
                are then lost when the process exits — logged as an error).
        """
        self._db = db
        self._interval_min = interval_min
        self._interval_delta = timedelta(minutes=interval_min)
        self._token_to_stock_id = token_to_stock_id or {}
        self._spill_path = Path(spill_path) if spill_path else None
        # Where spills are written this run (differs from _spill_path only
        # while the main file is blocked).
        self._active_spill_path: Path | None = self._spill_path
        # Spill files that could not be loaded — never touched again.
        self._blocked_spill_files: list[Path] = []
        # Spill files loaded OK this run; removed once the active file has
        # been rewritten (their candles are then either saved or in it).
        self._recovered_files: list[Path] = []

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

        # Last cumulative day-volume reading per token. Used to turn
        # SmartAPI's running total into per-candle increments.
        self._last_cum_volume: dict[str, int] = {}

        # Completed candles waiting to be flushed
        self._completed: list[CandleBucket] = []
        self._completed_lock = threading.Lock()

        # Serializes whole flush attempts (take → write → re-queue) so the
        # background loop and a manual flush_all() cannot interleave.
        self._flush_lock = threading.Lock()

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        self._running = False
        self._stop_event = threading.Event()

        # Tokens already reported as unmapped (avoid log spam every flush)
        self._unmapped_warned: set[str] = set()

        # Stats
        self._candles_flushed: int = 0
        self._ticks_processed: int = 0
        self._flush_failures: int = 0
        self._last_flush_error: str | None = None
        self._recovered_candles: int = 0
        self._spilled_candles: int = 0

    def start(self) -> None:
        """Recover spilled candles from a previous run, then start the
        background candle flush thread."""
        if self._running:
            return

        self.recover_pending()

        self._running = True
        self._stop_event.clear()
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

    def stop(self) -> int:
        """
        Stop the background thread, flush all remaining candles, and spill
        anything that still could not be saved to ``spill_path`` so it
        survives a restart.

        Returns:
            Number of candles left pending (0 means everything was written).
        """
        self._running = False
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)

        # Final flush
        flushed = self.flush_all()

        pending = self.pending_candles
        blocked_note = ""
        if self._blocked_spill_files:
            blocked_note = (
                f" | {len(self._blocked_spill_files)} spill file(s) could not be loaded at "
                f"start and were left untouched (candles inside NOT saved): "
                f"{[str(p) for p in self._blocked_spill_files]}"
            )
        if pending == 0:
            self._spilled_candles = 0
            self._remove_spill_file()
            message = (
                f"CandleAggregator stopped | final_flush={flushed}, "
                f"candles_flushed={self._candles_flushed}, "
                f"ticks_processed={self._ticks_processed:,}, nothing pending{blocked_note}"
            )
            if blocked_note:
                logger.error(message)
            else:
                logger.info(message)
            return 0

        unmapped = self.pending_unmapped
        failed = pending - unmapped
        spilled = self._spill_pending()
        detail = (
            f"{pending} candle(s) UNSAVED ({unmapped} awaiting stock_id, "
            f"{failed} after failed DB write"
            f"{' — last error: ' + self._last_flush_error if self._last_flush_error else ''})"
        )
        if spilled == pending:
            logger.warning(
                f"CandleAggregator stopped with {detail}; saved to {self._active_spill_path} "
                f"and will be retried on next start | candles_flushed={self._candles_flushed}"
                f"{blocked_note}"
            )
        else:
            logger.error(
                f"CandleAggregator stopped with {detail}; "
                f"{'no spill_path configured' if self._spill_path is None else 'spill failed'} "
                f"— these candles will be LOST | candles_flushed={self._candles_flushed}"
                f"{blocked_note}"
            )
        return pending

    def on_tick(self, tick: Tick) -> None:
        """
        Process a single tick — update or rotate the candle bucket.

        Thread-safe. Called from the WebSocket callback thread.

        Args:
            tick: Real-time tick from WebSocket.
        """
        self._ticks_processed += 1
        if not isfinite(tick.ltp) or tick.ltp <= 0 or tick.volume < 0:
            logger.debug(
                "Ignoring invalid OHLCV tick for {} at {}",
                tick.symbol,
                tick.timestamp,
            )
            return

        # Determine which candle interval this tick belongs to
        bucket_start = self._floor_to_interval(tick.timestamp)

        with self._lock:
            volume_delta, is_baseline = self._volume_increment(tick, bucket_start)
            existing = self._buckets.get(tick.token)

            if existing is None:
                # First tick for this stock — create new bucket
                bucket = CandleBucket(
                    token=tick.token,
                    symbol=tick.symbol,
                    interval_start=bucket_start,
                )
                bucket.update(tick, volume_delta, is_baseline)
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
                bucket.update(tick, volume_delta, is_baseline)
                self._buckets[tick.token] = bucket

            else:
                # Same interval — just update
                existing.update(tick, volume_delta, is_baseline)

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
        """
        Replace the token → stock_id mapping.

        Completed candles whose token had no mapping are held in the pending
        queue and will be written on the next flush once mapped.
        """
        self._token_to_stock_id = dict(mapping)
        self._unmapped_warned -= set(mapping)
        logger.debug(f"CandleAggregator token mapping updated: {len(mapping)} tokens")

    def reset_daily(self) -> int:
        """
        Prepare for a new trading day.

        1. Close any still-open buckets and try to flush everything pending.
        2. Keep whatever could not be saved (failed write, unmapped token)
           in the pending queue — it is retried on later flushes, so the
           previous day's candles are never discarded here.
        3. Clear the per-token cumulative-volume counters so the new day's
           first reading is treated as opening volume.

        Returns:
            Number of candles written during the reset flush.

        Thread-safety: the previous day's state is retired atomically under
        ``_lock`` *before* the (slow) database write. A tick arriving during
        that write therefore starts a fresh bucket and volume counter for
        the new day, which the reset never clears afterwards.
        """
        with self._lock:
            with self._completed_lock:
                for bucket in self._buckets.values():
                    if bucket.is_valid:
                        self._completed.append(bucket)
            self._buckets.clear()
            self._last_cum_volume.clear()
        self._unmapped_warned.clear()

        flushed = self._do_flush()

        pending = self.pending_candles
        if pending:
            logger.warning(
                f"CandleAggregator reset for new trading day: flushed {flushed}, "
                f"{pending} candle(s) from the previous day still pending "
                f"({self.pending_unmapped} unmapped)"
            )
        else:
            logger.info(f"CandleAggregator reset for new trading day (flushed {flushed})")
        return flushed

    # ── Internal ─────────────────────────────────────────

    def _floor_to_interval(self, dt: datetime) -> datetime:
        """
        Floor a datetime to the nearest candle interval boundary.

        Example (5-min): 10:22:45 → 10:20:00, 10:25:00 → 10:25:00
        """
        minute = (dt.minute // self._interval_min) * self._interval_min
        return dt.replace(minute=minute, second=0, microsecond=0)

    def _is_opening_interval(self, bucket_start: datetime) -> bool:
        """True if the bucket is the session's opening interval (09:15)."""
        opening = self._floor_to_interval(
            bucket_start.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute)
        )
        return bucket_start == opening

    def _volume_increment(self, tick: Tick, bucket_start: datetime) -> tuple[int, bool]:
        """
        Convert a cumulative day-volume reading into the increment that
        belongs to the current candle. Must be called under ``self._lock``.

        Returns:
            (volume_delta, is_baseline)
        """
        previous = self._last_cum_volume.get(tick.token)

        if previous is None:
            self._last_cum_volume[tick.token] = tick.volume
            if self._is_opening_interval(bucket_start):
                # Day counter started at 0 → everything so far is opening volume
                return tick.volume, False
            # Mid-session first sight: unknown how/when this volume traded
            return 0, True

        if tick.volume < previous:
            # Stale / out-of-order reading. Keep the last reliable counter so
            # the next higher reading is measured against it and volume is
            # not counted twice. A genuine new day goes through reset_daily().
            logger.debug(
                "Ignoring lower cumulative volume for {} ({} < {})",
                tick.symbol, tick.volume, previous,
            )
            return 0, False

        self._last_cum_volume[tick.token] = tick.volume
        return tick.volume - previous, False

    def _flush_loop(self) -> None:
        """Background thread: flush completed candles periodically."""
        while self._running:
            if self._stop_event.wait(FLUSH_INTERVAL_SEC):
                break
            if self._completed:
                self._do_flush()

    # ── Spill file (survive restarts) ────────────────────

    def recover_pending(self) -> int:
        """
        Load candles spilled by a previous run into the pending queue.

        Scans the main spill file plus any ``<name>.unsaved-*`` overflow
        files. Each file is loaded all-or-nothing: if it cannot be parsed,
        is for another interval, or contains a corrupt entry, nothing from
        it is loaded and the file is marked blocked — left untouched and
        reported until an operator deals with it.

        Files that load are NOT deleted here: they stay until their candles
        are actually written (see ``_sync_spill_file_locked``). Candles
        already pending (same token and interval_start) are not loaded twice.

        Returns:
            Number of candles recovered.
        """
        if self._spill_path is None:
            return 0

        total = 0
        for path in self._spill_candidates():
            if path in self._blocked_spill_files or path in self._recovered_files:
                continue
            buckets = self._load_spill_file(path)
            if buckets is None:
                self._blocked_spill_files.append(path)
                if path == self._spill_path:
                    self._active_spill_path = self._overflow_spill_path()
                continue

            with self._completed_lock:
                known = {b.key for b in self._completed}
                fresh = [b for b in buckets if b.key not in known]
                self._completed.extend(fresh)
            self._recovered_files.append(path)
            self._recovered_candles += len(fresh)
            total += len(fresh)
            if fresh:
                logger.warning(
                    f"CandleAggregator: recovered {len(fresh)} unsaved candle(s) from {path} "
                    f"({sum(1 for b in fresh if b.token not in self._token_to_stock_id)} "
                    f"still awaiting stock_id)"
                )

        if self._blocked_spill_files:
            logger.error(
                f"CandleAggregator: {len(self._blocked_spill_files)} spill file(s) could not "
                f"be loaded and were left untouched: "
                f"{[str(p) for p in self._blocked_spill_files]} — their candles are NOT "
                f"recovered; fix or move the file(s) and restart"
            )
        return total

    def _spill_candidates(self) -> list[Path]:
        """Main spill file followed by overflow files, oldest first."""
        if self._spill_path is None:
            return []
        paths = [self._spill_path] if self._spill_path.exists() else []
        if self._spill_path.parent.exists():
            paths += sorted(self._spill_path.parent.glob(self._spill_path.name + ".unsaved-*"))
        return [p for p in paths if not p.name.endswith(".tmp")]

    def _overflow_spill_path(self) -> Path:
        """A fresh sibling file to use while the main spill file is blocked."""
        assert self._spill_path is not None
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S-%f")
        return self._spill_path.with_name(f"{self._spill_path.name}.unsaved-{stamp}")

    def _load_spill_file(self, path: Path) -> list[CandleBucket] | None:
        """Parse one spill file. Returns None (and logs) if it is not fully
        loadable — never a partial result."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"CandleAggregator: cannot read spill file {path}: {e}")
            return None

        if not isinstance(payload, dict) or payload.get("interval") != self._interval_label:
            logger.error(
                f"CandleAggregator: spill file {path} is for interval "
                f"{payload.get('interval') if isinstance(payload, dict) else None!r}, "
                f"expected {self._interval_label!r}"
            )
            return None

        buckets: list[CandleBucket] = []
        items = payload.get("candles")
        if not isinstance(items, list):
            logger.error(f"CandleAggregator: spill file {path} has no candle list")
            return None
        for index, item in enumerate(items):
            try:
                buckets.append(CandleBucket.from_dict(item))
            except Exception as e:
                logger.error(
                    f"CandleAggregator: spill file {path} entry #{index} is corrupt "
                    f"({e}); not loading any of its {len(items)} candle(s)"
                )
                return None
        return buckets

    def _spill_pending(self) -> int:
        """Write all pending candles to the spill file atomically (used by
        stop()). Returns the number of candles spilled (0 on failure / no
        path)."""
        with self._flush_lock:
            return self._write_spill_file_locked()

    def _sync_spill_file_locked(self) -> None:
        """
        After a successful write: if spill files were recovered or written
        this run, make the active file mirror what is still pending (or
        delete it when nothing is). Caller must hold ``_flush_lock``. New
        candles completed during normal operation are only spilled at
        stop(); this keeps existing files truthful. Blocked files are never
        touched.
        """
        if self._active_spill_path is None:
            return
        if not self._recovered_files and not self._active_spill_path.exists():
            return
        if self.pending_candles == 0:
            self._remove_spill_file()
            self._spilled_candles = 0
        else:
            self._write_spill_file_locked()

    def _write_spill_file_locked(self) -> int:
        """Core spill writer; caller must hold ``_flush_lock``. Writes to the
        active path (an overflow file if the main one is blocked) and then
        drops any other recovered files, whose candles are now either saved
        or inside the freshly written file."""
        self._spilled_candles = 0
        if self._active_spill_path is None:
            return 0
        with self._completed_lock:
            candles = [b.to_dict() for b in self._completed]
        if not candles:
            self._remove_spill_file()
            return 0
        payload = {
            "interval": self._interval_label,
            "saved_at": datetime.now(IST).isoformat(),
            "candles": candles,
        }
        target = self._active_spill_path
        tmp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, target)
        except Exception as e:
            logger.error(f"CandleAggregator: failed to write spill file {target}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        self._spilled_candles = len(candles)
        self._drop_recovered_files(keep=target)
        return len(candles)

    def _remove_spill_file(self) -> None:
        """Remove the active spill file and any recovered files. Blocked
        files are never touched."""
        if self._active_spill_path is None:
            return
        self._unlink_spill(self._active_spill_path)
        self._drop_recovered_files(keep=None)

    def _drop_recovered_files(self, keep: Path | None) -> None:
        for path in list(self._recovered_files):
            if path != keep:
                self._unlink_spill(path)
        self._recovered_files = [p for p in self._recovered_files if p == keep]

    def _unlink_spill(self, path: Path) -> None:
        if path in self._blocked_spill_files:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.error(f"CandleAggregator: could not remove spill file {path}: {e}")

    def _do_flush(self) -> int:
        """
        Batch-insert completed candles into the database.

        A candle leaves the pending queue only once it has been written:

        - Candles whose token has no stock_id mapping are never written
          (candles_5m would accept a NULL stock_id, leaving an orphan row).
          They stay pending until a mapping arrives.
        - If the database write fails, the exact candles of that batch are
          put back in the pending queue and retried on the next flush.

        Flush attempts are serialized by ``_flush_lock`` so the background
        loop and a manual flush_all() can overlap without duplicating or
        losing candles.

        Returns:
            Number of candles successfully written.
        """
        with self._flush_lock:
            return self._flush_pending_locked()

    def _flush_pending_locked(self) -> int:
        """Body of _do_flush; caller must hold ``_flush_lock``."""
        with self._completed_lock:
            if not self._completed:
                return 0
            batch = list(self._completed)
            self._completed.clear()

        # Convert to tuples for DB insertion; hold back unmapped candles
        rows = []
        mapped: list[CandleBucket] = []
        unmapped: list[CandleBucket] = []
        for bucket in batch:
            stock_id = self._token_to_stock_id.get(bucket.token)
            if stock_id is None:
                unmapped.append(bucket)
                continue
            mapped.append(bucket)
            rows.append(bucket.to_tuple(stock_id))

        if unmapped:
            new_tokens = {b.token for b in unmapped} - self._unmapped_warned
            if new_tokens:
                self._unmapped_warned |= new_tokens
                logger.warning(
                    f"CandleAggregator: no stock_id for {len(new_tokens)} token(s) "
                    f"(e.g. {sorted(new_tokens)[:5]}) — holding {len(unmapped)} "
                    f"candle(s) until a mapping is available"
                )

        if not rows:
            self._requeue(unmapped)
            return 0

        try:
            from storage.models import CandleStore
            store = CandleStore(self._db)
            store.insert_batch(self._candle_interval, rows)

        except Exception as e:
            # Nothing was persisted: keep the whole batch (original order)
            # pending so the next flush retries exactly these candles.
            self._requeue(batch)
            self._flush_failures += 1
            self._last_flush_error = str(e)
            logger.error(
                f"CandleAggregator flush error: {e} | {len(rows)} candle(s) kept "
                f"pending for retry (failures={self._flush_failures})"
            )
            return 0

        # Written: only the unmapped candles remain pending
        self._requeue(unmapped)
        self._candles_flushed += len(rows)
        self._sync_spill_file_locked()
        if self._last_flush_error is not None:
            logger.info(
                f"CandleAggregator: flush recovered, wrote {len(rows)} candle(s) "
                f"after {self._flush_failures} failure(s)"
            )
            self._last_flush_error = None

        if self._candles_flushed % 500 == 0:
            logger.debug(
                f"CandleAggregator: flushed {len(rows)} candles "
                f"(total={self._candles_flushed:,})"
            )

        return len(rows)

    def _requeue(self, buckets: list[CandleBucket]) -> None:
        """Put buckets back at the front of the pending queue, keeping order."""
        if not buckets:
            return
        with self._completed_lock:
            self._completed[:0] = buckets

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
    def pending_tokens(self) -> set[str]:
        """Tokens of all pending (completed, unwritten) candles."""
        with self._completed_lock:
            return {b.token for b in self._completed}

    def flush_pending(self) -> int:
        """Write pending candles now (does not close open buckets)."""
        return self._do_flush()

    @property
    def pending_unmapped(self) -> int:
        """Pending candles held back because their token has no stock_id."""
        with self._completed_lock:
            return sum(1 for b in self._completed if b.token not in self._token_to_stock_id)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "active_buckets": len(self._buckets),
            "pending_candles": len(self._completed),
            "pending_unmapped": self.pending_unmapped,
            "candles_flushed": self._candles_flushed,
            "flush_failures": self._flush_failures,
            "last_flush_error": self._last_flush_error,
            "recovered_candles": self._recovered_candles,
            "spilled_candles": self._spilled_candles,
            "spill_path": str(self._active_spill_path) if self._active_spill_path else None,
            "blocked_spill_files": [str(p) for p in self._blocked_spill_files],
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
