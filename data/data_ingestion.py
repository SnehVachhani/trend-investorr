"""
Trend-Investorr: Data Ingestion Service

Background data pipeline that uses HistoricalFetcher to populate
per-interval candle tables in the database. NOT called at runtime.

Tables targeted:
    candles_1m, candles_5m, candles_15m, candles_1h, candles_1d, candles_1w

Responsibilities:
    1. Backfill — One-time: fetch N days of candle data → correct table
    2. Incremental Update — Nightly: fetch only today's new candles → append
    3. Aggregate — Compute daily candles from 5-min data → candles_1d
    4. Bhavcopy Persist — Store delivery data from NSE bhavcopy → candles_1d
    5. Warm-up Loader — Load volumes + candles from DB into memory for live trading

This service is the ONLY consumer of HistoricalFetcher.
The live tick handler NEVER touches HistoricalFetcher or makes API calls.
"""

from __future__ import annotations

import time
from datetime import datetime, date, timedelta
from typing import Any

from loguru import logger

from core.constants import CandleInterval, BACKFILL_DAYS, IST
from core.exceptions import DataFetchError
from data.models.market_data import Candle


class DataIngestionService:
    """
    Background data pipeline — fetches historical data and stores it in the DB.

    Uses a single CandleStore that dynamically targets the correct
    per-interval table (candles_5m, candles_1d, etc).

    Usage:
        ingestion = DataIngestionService(
            historical_fetcher=fetcher,
            candle_store=candle_store,
            stock_store=stock_store,
        )

        # Day 0: one-time backfill
        ingestion.backfill(days=90)

        # Nightly: append today's data
        ingestion.incremental_update()

        # Pre-market: load everything into memory
        volumes, candle_cache = ingestion.load_warmup_data()
    """

    def __init__(
        self,
        historical_fetcher: Any,
        candle_store: Any,
        stock_store: Any,
        coverage_store: Any = None,
    ):
        """
        Args:
            historical_fetcher: HistoricalFetcher instance (for Angel One API).
            candle_store: CandleStore instance (unified store for all interval tables).
            stock_store: StockStore instance (instrument master).
            coverage_store: CandleCoverageStore instance (tracks data ranges).
        """
        self._fetcher = historical_fetcher
        self._candle_store = candle_store
        self._stock_store = stock_store
        self._coverage = coverage_store
        self._indicator_store = None  # Set via set_indicator_store() after init

    # ── Backfill (One-Time) ──────────────────────────────

    def backfill(
        self,
        days: int = 90,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        exchange: str = "NSE",
        batch_pause: float = 0.1,
        max_workers: int = 5,
    ) -> dict[str, int]:
        """
        Smart backfill with concurrent fetching (max_workers threads).

        Checks candle_coverage first, only fetches what's missing.
        Uses ThreadPoolExecutor to fetch multiple stocks simultaneously.

        Args:
            days: Number of days of history required.
            interval: Candle interval to fetch.
            exchange: Stock exchange filter.
            batch_pause: Seconds to wait between stocks (courtesy delay).
            max_workers: Max concurrent API requests (default 5).

        Returns:
            Stats dict: {stocks_processed, candles_stored, skipped, errors}.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        stocks = self._stock_store.get_all_active(exchange=exchange)
        logger.info(
            f"Backfill started: {len(stocks)} stocks, {days} days, "
            f"{interval.value}, {max_workers} workers"
        )

        stats = {"stocks_processed": 0, "candles_stored": 0, "skipped": 0, "errors": 0}
        stats_lock = threading.Lock()
        processed_count = [0]  # mutable counter for progress logging

        now = datetime.now(IST)
        cutoff = now - timedelta(days=days)

        # Determine freshness cutoff:
        # Before market open (9:15 AM) → yesterday's data is the freshest available
        # After market open → need today's data
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        market_open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)

        if now < market_open_today:
            # Pre-market: previous trading day's data is still fresh
            freshness_cutoff = today_start - timedelta(days=1)
            # Handle weekends: Mon pre-market → Friday's data is fresh
            while freshness_cutoff.weekday() >= 5:  # Sat=5, Sun=6
                freshness_cutoff -= timedelta(days=1)
        else:
            # During/after market: need today's data
            freshness_cutoff = today_start

        def _process_stock(stock: dict) -> None:
            """Fetch and store candles for a single stock (runs in thread)."""
            try:
                # ── Check existing coverage ──
                if self._coverage:
                    existing = self._coverage.get(stock["id"], interval.value)
                    if existing and existing.get("earliest_ts") and existing.get("latest_ts"):
                        earliest = existing["earliest_ts"]
                        latest = existing["latest_ts"]
                        has_enough_history = earliest <= cutoff
                        is_fresh = latest >= freshness_cutoff
                        if has_enough_history and is_fresh:
                            with stats_lock:
                                stats["skipped"] += 1
                            return

                # ── Fetch from API ──
                candles = self._fetcher.get_candles(
                    symbol=stock["symbol"],
                    token=stock["token"],
                    days=days,
                    interval=interval,
                )

                if candles:
                    if interval == CandleInterval.ONE_DAY:
                        # Daily table has extra columns: delivery_qty, delivery_pct
                        rows = [
                            (
                                stock["id"],
                                c.timestamp,
                                c.open,
                                c.high,
                                c.low,
                                c.close,
                                c.volume,
                                c.vwap,
                                getattr(c, "delivery_qty", 0) or 0,
                                getattr(c, "delivery_pct", 0) or 0,
                            )
                            for c in candles
                        ]
                    else:
                        rows = [
                            (
                                stock["id"],
                                c.timestamp,
                                c.open,
                                c.high,
                                c.low,
                                c.close,
                                c.volume,
                                c.vwap,
                            )
                            for c in candles
                        ]
                    self._candle_store.insert_batch(interval, rows)

                    if self._coverage:
                        self._coverage.upsert(
                            stock_id=stock["id"],
                            interval=interval.value,
                            earliest=candles[0].timestamp,
                            latest=candles[-1].timestamp,
                            candle_count=len(candles),
                            source="angel_one",
                        )

                    with stats_lock:
                        stats["candles_stored"] += len(rows)

                with stats_lock:
                    stats["stocks_processed"] += 1
                    processed_count[0] += 1
                    total_done = processed_count[0] + stats["skipped"]
                    if total_done % 50 == 0:
                        logger.info(
                            f"Backfill progress: {total_done}/{len(stocks)} stocks | "
                            f"{stats['candles_stored']:,} stored, {stats['skipped']} skipped"
                        )

                time.sleep(batch_pause)

            except DataFetchError as e:
                with stats_lock:
                    stats["errors"] += 1
                    processed_count[0] += 1
                logger.warning(f"Backfill error for {stock['symbol']}: {e}")
            except Exception as e:
                with stats_lock:
                    stats["errors"] += 1
                    processed_count[0] += 1
                logger.error(f"Unexpected backfill error for {stock['symbol']}: {e}")

        # ── Run with thread pool (5 concurrent fetches) ──
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_process_stock, stock) for stock in stocks]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Thread error in backfill: {e}")

        # After backfill, aggregate into daily candles
        if interval != CandleInterval.ONE_DAY:
            logger.info("Backfill complete — aggregating daily candles...")
            self.aggregate_all_daily(
                source_interval=interval, days=days, exchange=exchange,
            )

        logger.info(
            f"Backfill finished: {stats['stocks_processed']} fetched, "
            f"{stats['skipped']} skipped, "
            f"{stats['candles_stored']:,} candles, {stats['errors']} errors"
        )
        return stats

    # ── Incremental Update (Nightly) ─────────────────────

    def incremental_update(
        self,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        exchange: str = "NSE",
    ) -> dict[str, int]:
        """
        Nightly incremental update: fetch today's intraday candles and append.

        Called by post_market() after market close.
        Only writes to the intraday table (e.g. candles_5m).
        Does NOT touch candles_1d — that's handled by store_bhavcopy().

        Returns:
            Stats dict: {stocks_updated, candles_added, errors}.
        """
        stocks = self._stock_store.get_all_active(exchange=exchange)
        logger.info(f"Incremental update started: {len(stocks)} stocks")

        stats = {"stocks_updated": 0, "candles_added": 0, "errors": 0}

        for stock in stocks:
            try:
                # Find the last stored timestamp for this stock
                last_ts = self._candle_store.get_latest_timestamp(
                    stock_id=stock["id"],
                    interval=interval,
                )

                # Default to today's market open if no data exists
                if last_ts is None:
                    last_ts = datetime.now(IST).replace(
                        hour=9, minute=15, second=0, microsecond=0,
                    )

                end = datetime.now(IST)

                # Skip if last_ts is already past market close today
                market_close_today = end.replace(
                    hour=15, minute=30, second=0, microsecond=0,
                )
                if last_ts >= market_close_today:
                    continue

                candles = self._fetcher.get_candles_range(
                    symbol=stock["symbol"],
                    token=stock["token"],
                    start=last_ts + timedelta(minutes=1),
                    end=end,
                    interval=interval,
                )

                if candles:
                    rows = [
                        (
                            stock["id"],
                            c.timestamp,
                            c.open,
                            c.high,
                            c.low,
                            c.close,
                            c.volume,
                            c.vwap,
                        )
                        for c in candles
                    ]
                    self._candle_store.insert_batch(interval, rows)
                    stats["candles_added"] += len(rows)
                    stats["stocks_updated"] += 1

                    # Update coverage tracking
                    if self._coverage and candles:
                        self._coverage.upsert(
                            stock_id=stock["id"],
                            interval=interval.value,
                            earliest=candles[0].timestamp,
                            latest=candles[-1].timestamp,
                            candle_count=len(candles),
                        )

                # Rate limit safety
                time.sleep(0.1)  # courtesy delay

            except DataFetchError as e:
                stats["errors"] += 1
                logger.debug(f"Incremental update error for {stock['symbol']}: {e}")
            except Exception as e:
                stats["errors"] += 1
                logger.error(f"Unexpected incremental error for {stock['symbol']}: {e}")

        logger.info(
            f"Incremental update finished: {stats['stocks_updated']} stocks, "
            f"{stats['candles_added']:,} candles, {stats['errors']} errors"
        )
        return stats

    # ── Aggregation: Intraday → Daily (candles_5m → candles_1d) ──

    def aggregate_daily_for_date(
        self,
        target_date: date,
        source_interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        exchange: str = "NSE",
    ) -> int:
        """
        Aggregate intraday candles into daily candles for a specific date.

        Reads from the source interval table (e.g. candles_5m) and
        writes aggregated OHLCV to candles_1d.

        Returns number of daily candles upserted.
        """
        stocks = self._stock_store.get_all_active(exchange=exchange)
        upserted = 0

        for stock in stocks:
            try:
                agg = self._candle_store.get_daily_aggregate(
                    stock_id=stock["id"],
                    source_interval=source_interval,
                    target_date=target_date,
                )

                if agg and agg.get("close"):
                    ts = datetime.combine(
                        target_date,
                        datetime.strptime("15:30", "%H:%M").time(),
                        tzinfo=IST,
                    )

                    self._candle_store.insert(
                        stock_id=stock["id"],
                        timestamp=ts,
                        interval=CandleInterval.ONE_DAY,
                        open_=float(agg["open"]),
                        high=float(agg["high"]),
                        low=float(agg["low"]),
                        close=float(agg["close"]),
                        volume=int(agg["volume"]),
                    )
                    upserted += 1

            except Exception as e:
                logger.debug(f"Aggregation error for {stock['symbol']} on {target_date}: {e}")

        logger.info(f"Daily aggregation for {target_date}: {upserted} candles upserted")
        return upserted

    def aggregate_all_daily(
        self,
        source_interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        days: int = 90,
        exchange: str = "NSE",
    ) -> int:
        """Aggregate all stored intraday data into daily candles (used after backfill)."""
        total = 0
        today = date.today()
        for day_offset in range(days):
            target = today - timedelta(days=day_offset)
            # Skip weekends
            if target.weekday() >= 5:
                continue
            total += self.aggregate_daily_for_date(
                target_date=target,
                source_interval=source_interval,
                exchange=exchange,
            )
        logger.info(f"Full daily aggregation: {total} candles across {days} days")
        return total

    def aggregate_weekly(self, exchange: str = "NSE") -> int:
        """
        Aggregate daily candles (candles_1d) into weekly candles (candles_1w).

        Each weekly candle = Monday 00:00 timestamp, aggregating Mon-Fri daily data.
        Called after daily backfill or as part of post-market.

        Returns number of weekly candles upserted.
        """
        stocks = self._stock_store.get_all_active(exchange=exchange)
        upserted = 0

        for stock in stocks:
            try:
                # Get all daily candles for this stock
                daily_rows = self._candle_store.get_candles(
                    stock_id=stock["id"],
                    interval=CandleInterval.ONE_DAY,
                    days=730,  # All available data
                )

                if not daily_rows:
                    continue

                # Group by ISO week (year, week_number)
                weeks: dict[tuple[int, int], list[dict]] = {}
                for row in daily_rows:
                    ts = row["timestamp"]
                    iso_year, iso_week, _ = ts.isocalendar()
                    key = (iso_year, iso_week)
                    if key not in weeks:
                        weeks[key] = []
                    weeks[key].append(row)

                # Build weekly candles
                for (iso_year, iso_week), days_in_week in weeks.items():
                    if not days_in_week:
                        continue

                    # Sort by timestamp
                    days_in_week.sort(key=lambda r: r["timestamp"])

                    # Weekly timestamp = Monday of that week
                    monday = days_in_week[0]["timestamp"].date()
                    monday -= timedelta(days=monday.weekday())  # Roll back to Monday
                    week_ts = datetime.combine(
                        monday, datetime.min.time(), tzinfo=IST,
                    )

                    self._candle_store.insert(
                        stock_id=stock["id"],
                        timestamp=week_ts,
                        interval=CandleInterval.ONE_WEEK,
                        open_=float(days_in_week[0]["open"]),
                        high=float(max(d["high"] for d in days_in_week)),
                        low=float(min(d["low"] for d in days_in_week)),
                        close=float(days_in_week[-1]["close"]),
                        volume=sum(int(d["volume"]) for d in days_in_week),
                    )
                    upserted += 1

            except Exception as e:
                logger.debug(f"Weekly aggregation error for {stock['symbol']}: {e}")

        logger.info(f"Weekly aggregation: {upserted} candles upserted")
        return upserted

    # ── Single-Stock Backfill (On-Demand) ─────────────────

    def backfill_stock(
        self,
        stock: dict,
        days: int = 365,
        intervals: list[CandleInterval] | None = None,
    ) -> dict[str, int]:
        """
        Backfill candle data for a SINGLE stock across specified intervals.

        Called on-demand when a new stock is added to a watchlist,
        so its chart data is available immediately without waiting
        for the next scheduled backfill cycle.

        Args:
            stock: Stock dict with keys: id, symbol, token.
            days: Number of days of history to fetch per interval.
            intervals: Which intervals to backfill. Defaults to all
                       API-fetchable intervals (1m, 5m, 15m, 1h, 1d).

        Returns:
            Stats dict: {candles_stored, intervals_done, errors}.
        """
        if intervals is None:
            intervals = [
                CandleInterval.FIVE_MINUTE,
                CandleInterval.FIFTEEN_MINUTE,
                CandleInterval.ONE_HOUR,
                CandleInterval.ONE_DAY,
            ]

        stats = {"candles_stored": 0, "intervals_done": 0, "errors": 0}

        for interval in intervals:
            # Use the per-interval day policy if available
            fetch_days = BACKFILL_DAYS.get(interval, days)

            try:
                candles = self._fetcher.get_candles(
                    symbol=stock["symbol"],
                    token=stock["token"],
                    days=fetch_days,
                    interval=interval,
                )

                if candles:
                    if interval == CandleInterval.ONE_DAY:
                        rows = [
                            (
                                stock["id"],
                                c.timestamp,
                                c.open,
                                c.high,
                                c.low,
                                c.close,
                                c.volume,
                                c.vwap,
                                getattr(c, "delivery_qty", 0) or 0,
                                getattr(c, "delivery_pct", 0) or 0,
                            )
                            for c in candles
                        ]
                    else:
                        rows = [
                            (
                                stock["id"],
                                c.timestamp,
                                c.open,
                                c.high,
                                c.low,
                                c.close,
                                c.volume,
                                c.vwap,
                            )
                            for c in candles
                        ]
                    self._candle_store.insert_batch(interval, rows)
                    stats["candles_stored"] += len(rows)

                    # Update coverage tracking
                    if self._coverage:
                        self._coverage.upsert(
                            stock_id=stock["id"],
                            interval=interval.value,
                            earliest=candles[0].timestamp,
                            latest=candles[-1].timestamp,
                            candle_count=len(candles),
                            source="on_demand",
                        )

                stats["intervals_done"] += 1

                # Rate limit courtesy
                time.sleep(0.15)

            except DataFetchError as e:
                stats["errors"] += 1
                logger.warning(
                    f"On-demand backfill error for {stock['symbol']} "
                    f"({interval.value}): {e}"
                )
            except Exception as e:
                stats["errors"] += 1
                logger.error(
                    f"Unexpected on-demand backfill error for {stock['symbol']} "
                    f"({interval.value}): {e}"
                )

        logger.info(
            f"On-demand backfill for {stock['symbol']}: "
            f"{stats['candles_stored']:,} candles across "
            f"{stats['intervals_done']} intervals, {stats['errors']} errors"
        )
        return stats

    # ── Backfill All Intervals ────────────────────────────

    def backfill_all_intervals(
        self,
        exchange: str = "NSE",
        batch_pause: float = 0.1,
    ) -> dict[str, dict]:
        """
        Run backfill for ALL intervals using the BACKFILL_DAYS policy.

        Fetches each interval with its configured history depth:
            1m → 1 day, 5m → 90 days, 15m → 30 days,
            1h → 90 days, 1d → 365 days, 1w → aggregated from 1d

        Returns:
            Dict of {interval: stats_dict} for each interval processed.
        """
        all_stats: dict[str, dict] = {}

        # Fetch API-based intervals (skip ONE_WEEK — it's aggregated)
        for interval, days in BACKFILL_DAYS.items():
            if interval == CandleInterval.ONE_WEEK:
                continue  # Weekly is aggregated, not fetched

            logger.info(f"Backfilling {interval.value}: {days} days")
            stats = self.backfill(
                days=days,
                interval=interval,
                exchange=exchange,
                batch_pause=batch_pause,
            )
            all_stats[interval.value] = stats

        # Aggregate weekly candles from daily data
        logger.info("Aggregating weekly candles from daily data...")
        weekly_count = self.aggregate_weekly(exchange=exchange)
        all_stats[CandleInterval.ONE_WEEK.value] = {
            "candles_stored": weekly_count, "source": "aggregated_from_daily",
        }

        logger.info(f"All-interval backfill complete: {list(all_stats.keys())}")
        return all_stats

    # ── Bhavcopy Persistence ─────────────────────────────

    def store_bhavcopy(self, candles: list[Candle]) -> int:
        """
        Persist bhavcopy candle data (with delivery info) into candles_1d.

        Bhavcopy provides delivery_qty and delivery_pct which are not
        available from the Angel One API.

        Args:
            candles: Candle dataclasses from BhavcopyFetcher.fetch()

        Returns:
            Number of rows upserted.
        """
        if not candles:
            return 0

        stored = 0
        for candle in candles:
            try:
                stock = self._stock_store.get_by_symbol(candle.symbol)
                if not stock:
                    continue

                self._candle_store.insert(
                    stock_id=stock["id"],
                    timestamp=candle.timestamp,
                    interval=CandleInterval.ONE_DAY,
                    open_=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    delivery_qty=candle.delivery_qty,
                    delivery_pct=candle.delivery_pct,
                )
                stored += 1

                # Update coverage for daily data
                if self._coverage:
                    self._coverage.upsert(
                        stock_id=stock["id"],
                        interval=CandleInterval.ONE_DAY.value,
                        earliest=candle.timestamp,
                        latest=candle.timestamp,
                        candle_count=1,
                        source="bhavcopy",
                    )

            except Exception as e:
                logger.debug(f"Bhavcopy store error for {candle.symbol}: {e}")

        logger.info(f"Bhavcopy persisted: {stored}/{len(candles)} stocks stored to candles_1d")
        return stored

    # ── Pre-Market Warm-up Loader ────────────────────────

    def load_warmup_data(self, days: int = 90) -> tuple[dict[str, tuple[int, int]], dict[str, list[Candle]]]:
        """
        Load all data needed for live trading from the DB into memory.

        Called at 9:00 AM during scanner_warmup(). Makes ZERO API calls.

        Returns:
            volumes: {token: (avg_30d, avg_90d)} for VolumeScanner
            candle_cache: {token: [Candle]} for Analyzer (daily candles, sorted ASC)
        """
        # 1. Load volume baselines from candles_1d
        volumes = self._candle_store.get_avg_volumes_bulk()
        logger.info(f"Warm-up: loaded volume baselines for {len(volumes)} stocks")

        # 2. Load daily candles for analyzer from candles_1d
        bulk_candles = self._candle_store.get_daily_candles_bulk(days=days)
        stocks = self._stock_store.get_all_active()

        # Build stock_id -> token map
        id_to_token: dict[int, str] = {}
        id_to_symbol: dict[int, str] = {}
        for stock in stocks:
            id_to_token[stock["id"]] = stock["token"]
            id_to_symbol[stock["id"]] = stock["symbol"]

        # Convert DB rows into Candle dataclasses keyed by token
        candle_cache: dict[str, list[Candle]] = {}
        for stock_id, rows in bulk_candles.items():
            token = id_to_token.get(stock_id)
            symbol = id_to_symbol.get(stock_id, "")
            if not token:
                continue

            candle_list = []
            for row in rows:
                try:
                    candle_list.append(Candle(
                        symbol=symbol,
                        timestamp=row["timestamp"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row["volume"]),
                        interval=CandleInterval.ONE_DAY,
                        delivery_qty=int(row.get("delivery_qty", 0) or 0),
                        delivery_pct=float(row.get("delivery_pct", 0) or 0),
                    ))
                except (ValueError, KeyError, TypeError):
                    continue

            if candle_list:
                candle_cache[token] = candle_list

        logger.info(
            f"Warm-up: loaded daily candle cache for {len(candle_cache)} stocks | "
            f"total candles in memory: {sum(len(v) for v in candle_cache.values()):,}"
        )

        # 3. Load pre-computed technical indicators
        indicator_cache: dict[str, Any] = {}
        if self._indicator_store:
            try:
                indicator_rows = self._indicator_store.get_bulk_latest()
                for row in indicator_rows:
                    sid = row.get("stock_id")
                    token = id_to_token.get(sid)
                    if not token:
                        continue
                    indicator_cache[token] = self._row_to_indicators(row)
                logger.info(f"Warm-up: loaded pre-computed indicators for {len(indicator_cache)} stocks")
            except Exception as e:
                logger.warning(f"Warm-up: failed to load indicators: {e} — continuing without")

        return volumes, candle_cache, indicator_cache

    # ── Fetch Older Data (Gap Filling) ───────────────────

    def fetch_older(
        self,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        required_days: int = 90,
        exchange: str = "NSE",
        batch_pause: float = 0.5,
    ) -> dict[str, int]:
        """
        Fetch older historical data to fill coverage gaps.

        Uses candle_coverage to find stocks that have less than
        required_days of data, then fetches only the missing range.

        Example:
            Stock RELIANCE has data from Apr 1 to May 2 (31 days).
            required_days=90 → needs 59 more days.
            Fetches Jan 1 to Mar 31 and prepends to the table.

        Returns:
            Stats dict: {stocks_filled, candles_added, already_complete, errors}.
        """
        if not self._coverage:
            logger.warning("Cannot fetch older data — no coverage store configured")
            return {"stocks_filled": 0, "candles_added": 0, "already_complete": 0, "errors": 0}

        gaps = self._coverage.find_gaps(
            interval=interval.value,
            required_days=required_days,
            exchange=exchange,
        )

        if not gaps:
            logger.info(f"No coverage gaps found for {interval.value} — all stocks have {required_days}+ days")
            return {"stocks_filled": 0, "candles_added": 0, "already_complete": len(self._stock_store.get_all_active(exchange=exchange)), "errors": 0}

        logger.info(f"Found {len(gaps)} stocks with coverage gaps for {interval.value}")

        stats = {"stocks_filled": 0, "candles_added": 0, "already_complete": 0, "errors": 0}

        for gap in gaps:
            try:
                if gap.get("earliest_ts"):
                    # Has some data — fetch before the earliest timestamp
                    fetch_end = gap["earliest_ts"]
                    gap_days = gap.get("gap_days", required_days)
                    fetch_days = min(gap_days + 5, required_days)  # +5 buffer
                else:
                    # No data at all — full backfill
                    fetch_end = datetime.now(IST)
                    fetch_days = required_days

                candles = self._fetcher.get_candles(
                    symbol=gap["symbol"],
                    token=gap["token"],
                    days=fetch_days,
                    interval=interval,
                )

                if candles:
                    rows = [
                        (
                            gap["stock_id"],
                            c.timestamp,
                            c.open,
                            c.high,
                            c.low,
                            c.close,
                            c.volume,
                            c.vwap,
                        )
                        for c in candles
                    ]
                    self._candle_store.insert_batch(interval, rows)
                    stats["candles_added"] += len(rows)
                    stats["stocks_filled"] += 1

                    # Update coverage
                    self._coverage.upsert(
                        stock_id=gap["stock_id"],
                        interval=interval.value,
                        earliest=candles[0].timestamp,
                        latest=candles[-1].timestamp,
                        candle_count=len(candles),
                    )

                    logger.debug(
                        f"Gap filled: {gap['symbol']} — "
                        f"{candles[0].timestamp.date()} to {candles[-1].timestamp.date()} "
                        f"({len(candles)} candles)"
                    )

                time.sleep(batch_pause)

            except DataFetchError as e:
                stats["errors"] += 1
                logger.debug(f"Gap fill error for {gap['symbol']}: {e}")
            except Exception as e:
                stats["errors"] += 1
                logger.error(f"Unexpected gap fill error for {gap['symbol']}: {e}")

        logger.info(
            f"Gap filling complete: {stats['stocks_filled']} stocks filled, "
            f"{stats['candles_added']:,} candles added, {stats['errors']} errors"
        )
        return stats

    # ── Indicator Store Accessor ──────────────────────────

    def set_indicator_store(self, indicator_store) -> None:
        """Set the indicator store after construction (avoids circular init)."""
        self._indicator_store = indicator_store

    # ── Technical Indicator Computation (Post-Market) ────

    def compute_and_store_indicators(
        self,
        target_date: date | None = None,
        exchange: str = "NSE",
    ) -> dict[str, int]:
        """
        Compute ALL technical indicators for ALL active stocks and persist to DB.

        Called once per day during post_market() at ~4:00 PM.
        Pure CPU math — no API calls. Typical runtime: 30-60 seconds.

        Flow:
            For each stock:
                1. Load 365 daily candles from candles_1d (already in DB)
                2. Run IndicatorEngine.compute_all() — RSI, EMA, MACD, ATR, etc.
                3. Upsert result into technical_indicators table

        Args:
            target_date: Date to compute indicators for (defaults to today).
            exchange: Exchange to filter stocks.

        Returns:
            Stats dict: {computed, skipped, errors}.
        """
        from engine.indicator_engine import IndicatorEngine
        from data.models.market_data import TechnicalIndicators

        if not self._indicator_store:
            logger.warning("Cannot compute indicators — no indicator_store configured")
            return {"computed": 0, "skipped": 0, "errors": 0}

        if target_date is None:
            target_date = date.today()

        stocks = self._stock_store.get_all_active(exchange=exchange)
        stats = {"computed": 0, "skipped": 0, "errors": 0}

        logger.info(
            f"Computing technical indicators for {len(stocks)} stocks | "
            f"date={target_date}"
        )

        batch: list[TechnicalIndicators] = []

        for stock in stocks:
            try:
                # Load daily candles (up to 365 days)
                rows = self._candle_store.get_candles(
                    stock_id=stock["id"],
                    interval=CandleInterval.ONE_DAY,
                    days=365,
                )

                if not rows or len(rows) < 14:
                    stats["skipped"] += 1
                    continue

                # Convert DB rows to Candle dataclasses (ascending order)
                candles = []
                for row in reversed(rows):  # DB returns DESC, we need ASC
                    try:
                        candles.append(Candle(
                            symbol=stock["symbol"],
                            timestamp=row["timestamp"],
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=int(row["volume"]),
                            interval=CandleInterval.ONE_DAY,
                            delivery_qty=int(row.get("delivery_qty", 0) or 0),
                            delivery_pct=float(row.get("delivery_pct", 0) or 0),
                        ))
                    except (ValueError, KeyError, TypeError):
                        continue

                if len(candles) < 14:
                    stats["skipped"] += 1
                    continue

                # Compute all indicators
                indicators = IndicatorEngine.compute_all(
                    candles=candles,
                    stock_id=stock["id"],
                    target_date=target_date,
                )
                batch.append(indicators)

                # Flush in batches of 100 to avoid memory pressure
                if len(batch) >= 100:
                    stored = self._indicator_store.upsert_batch(batch)
                    stats["computed"] += stored
                    batch.clear()

            except Exception as e:
                stats["errors"] += 1
                logger.debug(f"Indicator error for {stock['symbol']}: {e}")

        # Flush remaining
        if batch:
            stored = self._indicator_store.upsert_batch(batch)
            stats["computed"] += stored

        logger.info(
            f"Indicator computation complete: "
            f"{stats['computed']} computed, {stats['skipped']} skipped, "
            f"{stats['errors']} errors"
        )
        return stats

    def load_indicators_for_warmup(
        self,
        stock_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """
        Load pre-computed indicators from DB into memory.

        Called during 9:00 AM scanner_warmup().

        Args:
            stock_ids: Optional filter. None = all stocks.

        Returns:
            Dict of {token: TechnicalIndicators} for hot path access.
        """
        if not self._indicator_store:
            return {}

        rows = self._indicator_store.get_bulk_latest(stock_ids)

        stocks = self._stock_store.get_all_active()
        id_to_token = {s["id"]: s["token"] for s in stocks}

        result: dict[str, Any] = {}
        for row in rows:
            token = id_to_token.get(row.get("stock_id"))
            if not token:
                continue
            result[token] = self._row_to_indicators(row)

        logger.info(f"Indicator warm-up: loaded {len(result)} stock indicators from DB")
        return result

    @staticmethod
    def _row_to_indicators(row: dict) -> Any:
        """Convert a DB row dict into a TechnicalIndicators dataclass."""
        from data.models.market_data import TechnicalIndicators

        return TechnicalIndicators(
            stock_id=row.get("stock_id", 0),
            date=row.get("date", date.today()),
            sma_20=float(row.get("sma_20") or 0),
            sma_50=float(row.get("sma_50") or 0),
            sma_200=float(row.get("sma_200") or 0),
            ema_20=float(row.get("ema_20") or 0),
            ema_50=float(row.get("ema_50") or 0),
            supertrend=float(row.get("supertrend") or 0),
            supertrend_direction=int(row.get("supertrend_direction") or 0),
            rsi_14=float(row.get("rsi_14") or 50),
            macd_line=float(row.get("macd_line") or 0),
            macd_signal=float(row.get("macd_signal") or 0),
            macd_histogram=float(row.get("macd_histogram") or 0),
            adx_14=float(row.get("adx_14") or 0),
            atr_14=float(row.get("atr_14") or 0),
            bollinger_upper=float(row.get("bollinger_upper") or 0),
            bollinger_lower=float(row.get("bollinger_lower") or 0),
            bollinger_width=float(row.get("bollinger_width") or 0),
            obv=int(row.get("obv") or 0),
            vwap_daily=float(row.get("vwap_daily") or 0),
            delivery_pct=float(row.get("delivery_pct") or 0),
            pivot_point=float(row.get("pivot_point") or 0),
            pivot_r1=float(row.get("pivot_r1") or 0),
            pivot_r2=float(row.get("pivot_r2") or 0),
            pivot_s1=float(row.get("pivot_s1") or 0),
            pivot_s2=float(row.get("pivot_s2") or 0),
            support_level=float(row.get("support_level") or 0),
            resistance_level=float(row.get("resistance_level") or 0),
            high_52w=float(row.get("high_52w") or 0),
            low_52w=float(row.get("low_52w") or 0),
        )
