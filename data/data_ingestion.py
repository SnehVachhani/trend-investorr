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
from datetime import datetime, date, timedelta, time as dt_time
from typing import Any, Sequence

from loguru import logger

from core.constants import (
    BACKFILL_DAYS,
    CandleInterval,
    Exchange,
    IST,
    MARKET_OPEN,
    MARKET_CLOSE,
    NSE_HOLIDAYS_2026,
)
from core.exceptions import DataFetchError
from data.models.market_data import Candle


_STOCK_EXCHANGES = (Exchange.NSE.value, Exchange.BSE.value)


def _normalise_stock_exchanges(
    exchanges: str | Exchange | Sequence[str | Exchange],
) -> tuple[str, ...]:
    """Resolve the supported equity exchanges in stable priority order."""
    if isinstance(exchanges, (str, Exchange)):
        values = [exchanges]
    else:
        values = list(exchanges)

    resolved: list[str] = []
    for value in values:
        name = value.value if isinstance(value, Exchange) else str(value)
        name = name.strip().upper()
        if name in {"ALL", "*"}:
            for supported in _STOCK_EXCHANGES:
                if supported not in resolved:
                    resolved.append(supported)
        elif name in _STOCK_EXCHANGES:
            if name not in resolved:
                resolved.append(name)
        else:
            raise ValueError(f"Unsupported stock exchange for candle sync: {name}")

    if not resolved:
        raise ValueError("At least one stock exchange is required for candle sync")
    return tuple(resolved)


def _combine_exchange_stats(by_exchange: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Keep per-exchange detail and expose summed counters at interval level."""
    totals: dict[str, int | float] = {}
    for stats in by_exchange.values():
        for key, value in stats.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {**totals, "by_exchange": by_exchange}


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

    @classmethod
    def _freshness_cutoff(
        cls,
        now: datetime,
        interval: CandleInterval = CandleInterval.ONE_DAY,
    ) -> datetime:
        """Return the last expected candle timestamp for a completed session.

        A date-only cutoff incorrectly marked an intraday series fresh as soon
        as it had any candle on the latest trading date. For intraday intervals
        require the final full bar of that session; daily candles are stamped
        at midnight or the close, so the trading date is sufficient.
        """
        candidate = now.date()
        if now.timetz().replace(tzinfo=None) < MARKET_CLOSE:
            candidate -= timedelta(days=1)

        holidays = set(NSE_HOLIDAYS_2026)
        while candidate.weekday() >= 5 or candidate.isoformat() in holidays:
            candidate -= timedelta(days=1)

        if interval == CandleInterval.ONE_DAY:
            expected_time = dt_time.min
        else:
            expected_time = cls._final_session_bar_time(interval)
        return datetime.combine(candidate, expected_time, tzinfo=IST)

    @classmethod
    def _session_lookback_start(cls, now: datetime, lookback_sessions: int) -> datetime:
        """Start at midnight of the earliest completed session in the window."""
        candidate = cls._freshness_cutoff(now, CandleInterval.ONE_DAY).date()
        holidays = set(NSE_HOLIDAYS_2026)
        for _ in range(max(1, lookback_sessions) - 1):
            candidate -= timedelta(days=1)
            while candidate.weekday() >= 5 or candidate.isoformat() in holidays:
                candidate -= timedelta(days=1)
        return datetime.combine(candidate, dt_time.min, tzinfo=IST)

    @staticmethod
    def _final_session_bar_time(interval: CandleInterval) -> dt_time:
        """Return the final timestamp used by SmartAPI for an NSE session."""
        # The provider stamps its last hourly NSE candle at 15:15. The final
        # hourly bar is shortened to the exchange close rather than stamped
        # at 14:30; stored provider timestamps confirm this convention.
        if interval == CandleInterval.ONE_HOUR:
            return dt_time(15, 15)
        return (
            datetime.combine(date.min, MARKET_CLOSE)
            - DataIngestionService._interval_step(interval)
        ).time()

    def backfill(
        self,
        days: int = 90,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        exchange: str = "NSE",
        batch_pause: float = 0.1,
        max_workers: int = 5,
        max_stock_retries: int = 3,
        aggregate_daily: bool = True,
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
            max_stock_retries: Additional whole-stock retry rounds after a
                range fetch fails.
            aggregate_daily: Rebuild daily candles after this standalone
                backfill. Full multi-interval syncs disable this because the
                same aggregation would otherwise run repeatedly.

        Returns:
            Stats dict with fetched, skipped, no-data, retry, and error counts.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        stocks = self._stock_store.get_all_active(exchange=exchange)
        logger.info(
            f"Backfill started: {len(stocks)} stocks, {days} days, "
            f"{interval.value}, {max_workers} workers"
        )

        stats = {
            "stocks_processed": 0,
            "candles_stored": 0,
            "skipped": 0,
            "no_data": 0,
            "retry_attempts": 0,
            "errors": 0,
        }
        processed_count = [0]  # mutable counter for progress logging

        now = datetime.now(IST)
        cutoff = now - timedelta(days=days)

        # Use the last completed exchange trading date. This prevents a
        # weekend/holiday from being treated as a missing candle date.
        freshness_cutoff = self._freshness_cutoff(now, interval)

        interval_step = self._interval_step(interval)
        api_exchange = Exchange(exchange) if isinstance(exchange, str) else exchange
        full_history_request = (
            interval == CandleInterval.ONE_DAY
            and days >= BACKFILL_DAYS[CandleInterval.ONE_DAY]
        )

        def _missing_ranges(
            existing: dict | None,
        ) -> list[tuple[datetime, datetime, bool]]:
            """Return only uncovered older/newer ranges.

            The coverage table already tells us the boundaries of data in the
            candle table.  Do not request the whole policy window when only
            the older edge or the latest edge is missing.

            The final boolean identifies a range that checks the oldest
            boundary.  A successful empty result for that range proves a
            newer listing has no older candles and can be marked complete.
            """
            if not existing:
                return [(cutoff, now, True)]

            earliest = existing.get("earliest_ts")
            latest = existing.get("latest_ts")
            if not earliest or not latest:
                return [(cutoff, now, True)]

            ranges: list[tuple[datetime, datetime, bool]] = []

            if earliest > cutoff:
                older_end = earliest - interval_step
                if cutoff < older_end:
                    ranges.append((cutoff, older_end, True))

            if latest < freshness_cutoff:
                newer_start = max(latest + interval_step, cutoff)
                if newer_start < now:
                    ranges.append((newer_start, now, False))

            return ranges

        def _fetch_range(
            stock: dict,
            range_start: datetime,
            range_end: datetime,
        ) -> list[Candle]:
            """Fetch an exact missing range, with a test-friendly fallback."""
            range_fetcher = getattr(self._fetcher, "get_candles_range", None)
            if range_fetcher:
                return range_fetcher(
                    symbol=stock["symbol"],
                    token=stock["token"],
                    start=range_start,
                    end=range_end,
                    interval=interval,
                    exchange=api_exchange,
                )

            # Lightweight fakes and older fetcher adapters may only expose
            # get_candles(). Preserve compatibility while still limiting the
            # requested window to the missing range.
            requested_days = max(1, (range_end - range_start).days + 1)
            return self._fetcher.get_candles(
                symbol=stock["symbol"],
                token=stock["token"],
                days=requested_days,
                interval=interval,
                exchange=exchange,
            )

        def _process_stock(stock: dict) -> tuple[str, int, str]:
            """Fetch only missing ranges for one stock."""
            try:
                # ── Check existing coverage ──
                existing = None
                if self._coverage:
                    existing = self._coverage.get(stock["id"], interval.value)
                    if existing and existing.get("earliest_ts") and existing.get("latest_ts"):
                        earliest = existing["earliest_ts"]
                        latest = existing["latest_ts"]
                        # A successful full-lifetime request can prove that a
                        # newer listing has no older candles. Do not fetch it
                        # on every startup just because it is less than 7300
                        # days old.
                        has_enough_history = bool(existing.get("history_complete")) or earliest <= cutoff
                        is_fresh = latest >= freshness_cutoff
                        if has_enough_history and is_fresh:
                            return "skipped", 0, ""

                    # A valid empty response means the requested window was
                    # checked successfully but contained no market candles
                    # (weekend, exchange holiday, or an instrument with no
                    # history). For a partial coverage row we must still
                    # inspect the missing older boundary once; the previous
                    # empty result might have been for the latest boundary.
                    last_check = self._coverage.get_last_check(
                        stock["id"], interval.value,
                    )
                    if (
                        last_check
                        and last_check.get("last_result") == "empty"
                        and last_check.get("last_checked_at")
                        and last_check["last_checked_at"] >= freshness_cutoff
                        and (
                            not existing
                            or bool(existing.get("history_complete"))
                        )
                    ):
                        return "no_data", 0, ""

                ranges = _missing_ranges(existing)
                if not ranges:
                    return "skipped", 0, ""

                total_stored = 0
                saw_data = False

                # Store each range as it succeeds. If a later range fails,
                # the retry pass sees the updated coverage and retries only
                # the still-missing range.
                for range_start, range_end, checks_oldest in ranges:
                    candles = _fetch_range(stock, range_start, range_end)
                    if not candles:
                        if checks_oldest and self._coverage and existing:
                            self._coverage.mark_history_complete(
                                stock_id=stock["id"],
                                interval=interval.value,
                            )
                        continue

                    candles.sort(key=lambda candle: candle.timestamp)
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
                    total_stored += len(rows)
                    saw_data = True

                    if self._coverage:
                        self._coverage.upsert(
                            stock_id=stock["id"],
                            interval=interval.value,
                            earliest=candles[0].timestamp,
                            latest=candles[-1].timestamp,
                            candle_count=len(candles),
                            source="angel_one",
                            history_complete=(
                                True if full_history_request and checks_oldest else None
                            ),
                        )

                if self._coverage:
                    self._coverage.record_check(
                        stock_id=stock["id"],
                        interval=interval.value,
                        result="data" if saw_data else "empty",
                    )

                if not saw_data:
                    return "no_data", 0, ""

                time.sleep(batch_pause)
                return "processed", total_stored, ""

            except DataFetchError as e:
                return "error", 0, str(e)
            except Exception as e:
                return "error", 0, str(e)

        def _run_batch(batch: list[dict], final_round: bool) -> list[dict]:
            """Run one concurrent batch and return only failed stocks."""
            failed: list[dict] = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_process_stock, stock): stock
                    for stock in batch
                }
                for future in as_completed(futures):
                    stock = futures[future]
                    try:
                        status, stored, error = future.result()
                    except Exception as exc:  # defensive: worker should self-classify
                        status, stored, error = "error", 0, str(exc)

                    if status == "error":
                        if final_round:
                            stats["errors"] += 1
                            processed_count[0] += 1
                            if self._coverage:
                                self._coverage.record_check(
                                    stock_id=stock["id"],
                                    interval=interval.value,
                                    result="error",
                                    error=error,
                                )
                            logger.error(
                                f"Backfill permanently failed for {stock['symbol']} "
                                f"after stock-level retries: {error}"
                            )
                        else:
                            failed.append(stock)
                        continue

                    if status == "processed":
                        stats["stocks_processed"] += 1
                        stats["candles_stored"] += stored
                    elif status == "skipped":
                        stats["skipped"] += 1
                    else:
                        stats["no_data"] += 1
                    processed_count[0] += 1

                    total_done = processed_count[0]
                    if total_done % 50 == 0:
                        logger.info(
                            f"Backfill progress: {total_done}/{len(stocks)} stocks | "
                            f"{stats['candles_stored']:,} stored, "
                            f"{stats['skipped']} skipped, {stats['no_data']} no-data, "
                            f"{len(failed)} retrying"
                        )
            return failed

        pending = _run_batch(stocks, final_round=max_stock_retries == 0)
        for retry_round in range(1, max_stock_retries + 1):
            if not pending:
                break
            stats["retry_attempts"] += len(pending)
            delay = min(60, 2 ** retry_round)
            logger.warning(
                f"Retrying {len(pending)} failed {interval.value} stocks "
                f"in {delay}s (round {retry_round}/{max_stock_retries})"
            )
            time.sleep(delay)
            pending = _run_batch(
                pending,
                final_round=retry_round == max_stock_retries,
            )

        # After a standalone backfill, aggregate into daily candles. The
        # all-interval startup path disables this to avoid repeating hundreds
        # of thousands of per-stock/day queries after every interval.
        if interval != CandleInterval.ONE_DAY and aggregate_daily:
            logger.info("Backfill complete — aggregating daily candles...")
            self.aggregate_all_daily(
                source_interval=interval, days=days, exchange=exchange,
            )

        logger.info(
            f"Backfill finished: {stats['stocks_processed']} fetched, "
            f"{stats['skipped']} skipped, "
            f"{stats['no_data']} no-data, {stats['candles_stored']:,} candles, "
            f"{stats['errors']} errors, {stats['retry_attempts']} stock retries"
        )
        return stats

    # ── Incremental Update (Nightly) ─────────────────────

    @staticmethod
    def _interval_step(interval: CandleInterval) -> timedelta:
        """Return the smallest timestamp step for an interval."""
        return {
            CandleInterval.ONE_MINUTE: timedelta(minutes=1),
            CandleInterval.FIVE_MINUTE: timedelta(minutes=5),
            CandleInterval.FIFTEEN_MINUTE: timedelta(minutes=15),
            CandleInterval.ONE_HOUR: timedelta(hours=1),
            CandleInterval.ONE_DAY: timedelta(days=1),
            CandleInterval.ONE_WEEK: timedelta(days=7),
        }.get(interval, timedelta(minutes=1))

    @classmethod
    def _get_incremental_start(
        cls,
        last_ts: datetime | None,
        end: datetime,
        interval: CandleInterval,
        max_history_days: int | None = None,
    ) -> datetime:
        """Choose the first timestamp that can still contain pending candles."""
        lookback_start = (
            end - timedelta(days=max_history_days)
            if max_history_days is not None
            else None
        )
        if last_ts is None:
            return lookback_start or end

        next_candle = last_ts + cls._interval_step(interval)
        if lookback_start is None:
            return next_candle
        return max(next_candle, lookback_start)

    @classmethod
    def _is_interval_current(
        cls,
        last_ts: datetime | None,
        end: datetime,
        interval: CandleInterval,
    ) -> bool:
        """Return whether an interval is complete after market close."""
        if last_ts is None:
            return False
        return last_ts >= cls._freshness_cutoff(end, interval)

    def incremental_update(
        self,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        exchange: str = "NSE",
        max_history_days: int | None = None,
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

                end = datetime.now(IST)

                if self._is_interval_current(last_ts, end, interval):
                    continue

                freshness_cutoff = self._freshness_cutoff(end, interval)
                if self._coverage:
                    last_check = self._coverage.get_last_check(
                        stock["id"], interval.value,
                    )
                    if (
                        last_check
                        and last_check.get("last_result") == "empty"
                        and last_check.get("last_checked_at")
                        and last_check["last_checked_at"] >= freshness_cutoff
                    ):
                        continue

                start = self._get_incremental_start(
                    last_ts=last_ts,
                    end=end,
                    interval=interval,
                    max_history_days=max_history_days,
                )

                candles = self._fetcher.get_candles_range(
                    symbol=stock["symbol"],
                    token=stock["token"],
                    start=start,
                    end=end,
                    interval=interval,
                    exchange=Exchange(exchange) if isinstance(exchange, str) else exchange,
                )

                if candles:
                    if interval == CandleInterval.ONE_DAY:
                        # candles_1d includes delivery fields in addition to
                        # the common OHLCV/VWAP columns. Keep incremental
                        # inserts aligned with CandleStore.insert_batch().
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

                if self._coverage:
                    self._coverage.record_check(
                        stock_id=stock["id"],
                        interval=interval.value,
                        result="data" if candles else "empty",
                    )

                # Rate limit safety
                time.sleep(0.1)  # courtesy delay

            except DataFetchError as e:
                stats["errors"] += 1
                if self._coverage:
                    self._coverage.record_check(
                        stock_id=stock["id"],
                        interval=interval.value,
                        result="error",
                        error=type(e).__name__,
                    )
                logger.debug(
                    "Incremental update error for {} ({})",
                    stock["symbol"],
                    type(e).__name__,
                )
            except Exception as e:
                stats["errors"] += 1
                if self._coverage:
                    self._coverage.record_check(
                        stock_id=stock["id"],
                        interval=interval.value,
                        result="error",
                        error=type(e).__name__,
                    )
                logger.error(
                    "Unexpected incremental error for {} ({})",
                    stock["symbol"],
                    type(e).__name__,
                )

        logger.info(
            f"Incremental update finished: {stats['stocks_updated']} stocks, "
            f"{stats['candles_added']:,} candles, {stats['errors']} errors"
        )
        return stats

    def sync_pending_all_intervals(
        self,
        exchange: str | Exchange | Sequence[str | Exchange] = "ALL",
    ) -> dict[str, dict]:
        """Fetch pending candles, finishing each exchange's 5m data first."""
        exchanges = _normalise_stock_exchanges(exchange)
        all_stats: dict[str, dict] = {}

        # Keep the primary five-minute stream first in the scheduled catch-up
        # too.  The insertion order of BACKFILL_DAYS is not an API guarantee,
        # and a long one-minute sweep should not delay the bars used by the
        # intraday backtests and volume-profile filters.
        interval_order = (
            CandleInterval.FIVE_MINUTE,
            CandleInterval.ONE_MINUTE,
            CandleInterval.FIFTEEN_MINUTE,
            CandleInterval.ONE_HOUR,
            CandleInterval.ONE_DAY,
        )
        for interval in interval_order:
            days = BACKFILL_DAYS[interval]

            logger.info(
                f"Synchronizing pending {interval.value} candles "
                f"(max lookback: {days} days)"
            )
            by_exchange: dict[str, dict[str, Any]] = {}
            for exchange_name in exchanges:
                stats = self.incremental_update(
                    interval=interval,
                    exchange=exchange_name,
                    max_history_days=days,
                )
                if interval == CandleInterval.FIVE_MINUTE:
                    stats.update(self.repair_recent_session_edge_gaps(
                        interval=interval,
                        exchange=exchange_name,
                        lookback_days=2,
                    ))
                by_exchange[exchange_name] = stats
            all_stats[interval.value] = _combine_exchange_stats(by_exchange)

        # One-minute gaps can help reconstruct recent five-minute edges. Run
        # this repair after both exchanges' primary 5m downloads and the 1m
        # catch-up, while preserving the 5m-first priority for provider calls.
        five_minute_by_exchange = all_stats[CandleInterval.FIVE_MINUTE.value]["by_exchange"]
        for exchange_name in exchanges:
            five_minute_by_exchange[exchange_name].update(
                self.repair_recent_five_minute_edges_from_one_minute(
                    exchange=exchange_name,
                    lookback_days=2,
                )
            )
        all_stats[CandleInterval.FIVE_MINUTE.value] = _combine_exchange_stats(
            five_minute_by_exchange,
        )

        logger.info("Aggregating weekly candles from synchronized daily data...")
        weekly_by_exchange = {
            exchange_name: {
                "candles_stored": self.aggregate_weekly(exchange=exchange_name),
            }
            for exchange_name in exchanges
        }
        all_stats[CandleInterval.ONE_WEEK.value] = {
            **_combine_exchange_stats(weekly_by_exchange),
            "source": "aggregated_from_daily",
        }
        logger.info("Pending candle synchronization complete for {}", ", ".join(exchanges))
        return all_stats

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
        if self._coverage:
            self._coverage.refresh_from_table(CandleInterval.ONE_DAY.value)

        logger.info(f"Full daily aggregation: {total} candles across {days} days")
        return total

    def aggregate_weekly(self, exchange: str = "NSE") -> int:
        """
        Aggregate daily candles (candles_1d) into weekly candles (candles_1w).

        Each weekly candle = Monday 00:00 timestamp, aggregating Mon-Fri daily data.
        Called after daily backfill or as part of post-market.

        Returns number of weekly candles upserted.
        """
        upserted = self._candle_store.aggregate_weekly_from_daily(
            days=730,
            exchange=exchange,
        )

        if self._coverage:
            self._coverage.refresh_from_table(CandleInterval.ONE_WEEK.value)

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
            stock: Stock dict with keys: id, symbol, token, and optionally
                exchange.  NSE remains the compatibility default for older
                callers that do not supply an exchange.
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

        exchange_name = str(stock.get("exchange", Exchange.NSE.value)).upper()
        try:
            exchange = Exchange(exchange_name)
        except ValueError:
            logger.warning(
                f"Unknown exchange {exchange_name!r} for {stock['symbol']}; "
                "using NSE for the on-demand backfill"
            )
            exchange = Exchange.NSE

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
                    exchange=exchange,
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
        exchange: str | Exchange | Sequence[str | Exchange] = "ALL",
        batch_pause: float = 0.1,
    ) -> dict[str, dict]:
        """
        Run backfill for ALL intervals using the BACKFILL_DAYS policy.

        Fetches each interval with its configured history depth:
            1m → 7 days, 5m → 1095 days, 15m → 30 days,
            1h → 90 days, 1d → lifetime window, 1w → aggregated from 1d

        Returns:
            Dict of {interval: stats_dict} for each interval processed.
        """
        exchanges = _normalise_stock_exchanges(exchange)
        all_stats: dict[str, dict] = {}

        # Repair the 5-minute series first because it feeds the intraday
        # backtests and volume profile. Previously current 1-minute gaps could
        # occupy most of the provider's rate-limited request window first.
        interval_order = (
            CandleInterval.FIVE_MINUTE,
            CandleInterval.ONE_MINUTE,
            CandleInterval.FIFTEEN_MINUTE,
            CandleInterval.ONE_HOUR,
            CandleInterval.ONE_DAY,
        )
        for interval in interval_order:
            days = BACKFILL_DAYS[interval]

            # Coverage metadata can predate the current candle tables (for
            # example after an import or an older sync that wrote candles
            # without updating candle_coverage).  Reconcile the boundaries
            # from the actual table before deciding which ranges are missing.
            # Without this, an existing multi-day history can look like a
            # short history and cause the backfill to download it again.
            if self._coverage:
                logger.info(
                    f"Reconciling {interval.value} coverage from stored candles"
                )
                self._coverage.refresh_from_table(interval.value)

            logger.info(f"Backfilling {interval.value}: {days} days across {', '.join(exchanges)}")
            by_exchange: dict[str, dict[str, Any]] = {}
            for exchange_name in exchanges:
                stats = self.backfill(
                    days=days,
                    interval=interval,
                    exchange=exchange_name,
                    batch_pause=batch_pause,
                    aggregate_daily=False,
                )
                if interval == CandleInterval.FIVE_MINUTE:
                    # Repair boundary holes before spending requests on lower
                    # priority candle intervals.
                    stats.update(self.repair_recent_session_edge_gaps(
                        interval=interval,
                        exchange=exchange_name,
                        lookback_days=2,
                    ))
                by_exchange[exchange_name] = stats
            all_stats[interval.value] = _combine_exchange_stats(by_exchange)

        # A complete 1m pass may provide source bars for any remaining recent
        # 5m gaps. This is a repair only; primary 5m fetching remains first.
        five_minute_by_exchange = all_stats[CandleInterval.FIVE_MINUTE.value]["by_exchange"]
        for exchange_name in exchanges:
            five_minute_by_exchange[exchange_name].update(
                self.repair_recent_five_minute_edges_from_one_minute(
                    exchange=exchange_name,
                    lookback_days=2,
                )
            )
        all_stats[CandleInterval.FIVE_MINUTE.value] = _combine_exchange_stats(
            five_minute_by_exchange,
        )

        # Aggregate weekly candles from daily data for every listed venue.
        logger.info("Aggregating weekly candles from daily data...")
        weekly_by_exchange = {
            exchange_name: {
                "candles_stored": self.aggregate_weekly(exchange=exchange_name),
            }
            for exchange_name in exchanges
        }
        all_stats[CandleInterval.ONE_WEEK.value] = {
            **_combine_exchange_stats(weekly_by_exchange),
            "source": "aggregated_from_daily",
        }

        logger.info(
            "All-interval backfill complete for {}: {}",
            ", ".join(exchanges), list(all_stats.keys()),
        )
        return all_stats

    def repair_recent_session_edge_gaps(
        self,
        *,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
        exchange: str = "NSE",
        lookback_days: int = 60,
        stock_ids: list[int] | None = None,
        batch_pause: float = 0.05,
    ) -> dict[str, int]:
        """Re-fetch stored sessions missing the opening or final full candle.

        Coverage min/max values cannot reveal a missing bar inside an otherwise
        covered range. This targeted repair checks recent session boundaries,
        fetches the whole affected session, and upserts it idempotently.
        """
        stats = {
            "edge_sessions_found": 0,
            "edge_sessions_attempted": 0,
            "edge_sessions_repaired": 0,
            "edge_sessions_unresolved": 0,
            "edge_missing_open": 0,
            "edge_missing_close": 0,
            "edge_candles_received": 0,
            "edge_errors": 0,
        }
        if self._coverage is None:
            logger.warning("Cannot repair session-edge gaps without candle coverage storage")
            stats["edge_errors"] = 1
            return stats

        interval_value = interval.value
        now = datetime.now(IST)
        lookback_start = self._session_lookback_start(now, lookback_days)
        gaps = self._coverage.find_recent_session_edge_gaps(
            interval=interval_value,
            start=lookback_start,
            end=now,
            exchange=exchange,
            stock_ids=stock_ids,
        )
        stats["edge_sessions_found"] = len(gaps)
        if not gaps:
            return stats

        expected_close_time = self._final_session_bar_time(interval)
        touched_stock_ids: set[int] = set()
        api_exchange = Exchange(exchange) if isinstance(exchange, str) else exchange

        for gap in gaps:
            stats["edge_sessions_attempted"] += 1
            session_date = gap["session_date"]
            session_start = datetime.combine(session_date, MARKET_OPEN, tzinfo=IST)
            session_end = datetime.combine(session_date, MARKET_CLOSE, tzinfo=IST)
            try:
                candles = self._fetcher.get_candles_range(
                    symbol=gap["symbol"],
                    token=gap["token"],
                    start=session_start,
                    end=session_end,
                    interval=interval,
                    exchange=api_exchange,
                )
                candles.sort(key=lambda candle: candle.timestamp)
                if candles:
                    rows = [
                        (
                            gap["stock_id"],
                            candle.timestamp,
                            candle.open,
                            candle.high,
                            candle.low,
                            candle.close,
                            candle.volume,
                            candle.vwap,
                        )
                        for candle in candles
                    ]
                    self._candle_store.insert_batch(interval, rows)
                    touched_stock_ids.add(gap["stock_id"])
                    stats["edge_candles_received"] += len(candles)

                fetched_times = {
                    candle.timestamp.astimezone(IST).time().replace(tzinfo=None)
                    for candle in candles
                }
                has_open = bool(gap["has_open"]) or MARKET_OPEN in fetched_times
                has_close = bool(gap["has_close"]) or expected_close_time in fetched_times
                if not has_open:
                    stats["edge_missing_open"] += 1
                if not has_close:
                    stats["edge_missing_close"] += 1
                if has_open and has_close:
                    stats["edge_sessions_repaired"] += 1
                else:
                    stats["edge_sessions_unresolved"] += 1
                time.sleep(batch_pause)
            except Exception as exc:
                stats["edge_errors"] += 1
                stats["edge_sessions_unresolved"] += 1
                logger.warning(
                    "Session-edge repair failed for {} on {} ({})",
                    gap["symbol"],
                    session_date,
                    interval_value,
                    type(exc).__name__,
                )
            if stats["edge_sessions_attempted"] % 25 == 0:
                logger.info(
                    "Session-edge repair {} progress: {}/{} sessions",
                    interval_value,
                    stats["edge_sessions_attempted"],
                    stats["edge_sessions_found"],
                )

        for stock_id in touched_stock_ids:
            self._coverage.refresh_stock_from_table(stock_id, interval_value)

        logger.info(
            "Session-edge repair {}: {}/{} repaired, {} unresolved, {} candles fetched, {} errors",
            interval_value,
            stats["edge_sessions_repaired"],
            stats["edge_sessions_found"],
            stats["edge_sessions_unresolved"],
            stats["edge_candles_received"],
            stats["edge_errors"],
        )
        return stats

    def repair_recent_five_minute_edges_from_one_minute(
        self,
        *,
        exchange: str = "NSE",
        lookback_days: int = 2,
        stock_ids: list[int] | None = None,
        fetch_missing_groups: bool = False,
    ) -> dict[str, int]:
        """Rebuild absent 5-minute session edges from complete stored 1-minute bars.

        A five-minute OHLCV bar is reconstructed only when all five underlying
        one-minute bars are present. This provides an exact aggregation for
        recent sessions where SmartAPI omitted the five-minute edge candle,
        without filling gaps with estimated prices or volume.
        """
        stats = {
            "minute_edge_sessions_found": 0,
            "minute_edge_groups_requested": 0,
            "minute_edge_bars_rebuilt": 0,
            "minute_edge_groups_incomplete": 0,
            "minute_edge_provider_groups_attempted": 0,
            "minute_edge_provider_bars_rebuilt": 0,
            "minute_edge_errors": 0,
        }
        if self._coverage is None:
            stats["minute_edge_errors"] = 1
            logger.warning(
                "Cannot rebuild five-minute edges from one-minute bars without coverage storage"
            )
            return stats

        now = datetime.now(IST)
        lookback_start = self._session_lookback_start(now, lookback_days)
        gaps = self._coverage.find_recent_session_edge_gaps(
            interval=CandleInterval.FIVE_MINUTE.value,
            start=lookback_start,
            end=now,
            exchange=exchange,
            stock_ids=stock_ids,
        )
        stats["minute_edge_sessions_found"] = len(gaps)

        requested: set[tuple[int, date, dt_time]] = set()
        expected_close = self._final_session_bar_time(CandleInterval.FIVE_MINUTE)
        for gap in gaps:
            if not gap["has_open"]:
                requested.add((gap["stock_id"], gap["session_date"], MARKET_OPEN))
            if not gap["has_close"]:
                requested.add((gap["stock_id"], gap["session_date"], expected_close))

        bars = sorted(requested, key=lambda item: (item[1], item[0], item[2]))
        stats["minute_edge_groups_requested"] = len(bars)
        if not bars:
            return stats

        try:
            aggregated = self._candle_store.aggregate_complete_one_minute_groups(bars)
            rows = [
                (
                    row["stock_id"],
                    row["timestamp"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row["vwap"],
                )
                for row in aggregated
            ]

            # For a specifically requested historical repair, SmartAPI may
            # still have the missing one-minute group even when it was never
            # stored locally. Fetch only that five-minute edge, and keep the
            # same strict all-five-minutes completeness requirement.
            if fetch_missing_groups and self._fetcher is not None:
                resolved = {
                    (row[0], row[1].astimezone(IST).date(), row[1].astimezone(IST).time().replace(tzinfo=None))
                    for row in rows
                }
                gap_by_stock_date = {
                    (gap["stock_id"], gap["session_date"]): gap
                    for gap in gaps
                }
                for stock_id, session_date, bar_start in bars:
                    key = (stock_id, session_date, bar_start)
                    if key in resolved:
                        continue
                    gap = gap_by_stock_date.get((stock_id, session_date))
                    if not gap:
                        continue
                    start = datetime.combine(session_date, bar_start, tzinfo=IST)
                    end = start + timedelta(minutes=5)
                    stats["minute_edge_provider_groups_attempted"] += 1
                    try:
                        candles = self._fetcher.get_candles_range(
                            symbol=gap["symbol"],
                            token=gap["token"],
                            start=start,
                            end=end,
                            interval=CandleInterval.ONE_MINUTE,
                            exchange=Exchange(exchange) if isinstance(exchange, str) else exchange,
                        )
                        minute_by_time = {
                            candle.timestamp.astimezone(IST).replace(second=0, microsecond=0): candle
                            for candle in candles
                        }
                        expected = [start + timedelta(minutes=index) for index in range(5)]
                        if not all(timestamp in minute_by_time for timestamp in expected):
                            if stats["minute_edge_provider_groups_attempted"] % 25 == 0:
                                logger.info(
                                    "One-minute edge fetch progress: {}/{} groups",
                                    stats["minute_edge_provider_groups_attempted"],
                                    len(bars) - len(aggregated),
                                )
                            continue
                        minute_group = [minute_by_time[timestamp] for timestamp in expected]
                        volume = sum(int(candle.volume) for candle in minute_group)
                        vwap = (
                            sum(float(candle.vwap or 0) * int(candle.volume) for candle in minute_group) / volume
                            if volume
                            else 0
                        )
                        rows.append(
                            (
                                stock_id,
                                start,
                                minute_group[0].open,
                                max(candle.high for candle in minute_group),
                                min(candle.low for candle in minute_group),
                                minute_group[-1].close,
                                volume,
                                vwap,
                            )
                        )
                        resolved.add(key)
                        stats["minute_edge_provider_bars_rebuilt"] += 1
                    except Exception as exc:
                        stats["minute_edge_errors"] += 1
                        logger.warning(
                            "Could not fetch one-minute edge group for {} on {} ({})",
                            gap["symbol"],
                            session_date,
                            type(exc).__name__,
                        )
                    if stats["minute_edge_provider_groups_attempted"] % 25 == 0:
                        logger.info(
                            "One-minute edge fetch progress: {}/{} groups",
                            stats["minute_edge_provider_groups_attempted"],
                            len(bars) - len(aggregated),
                        )

            if rows:
                self._candle_store.insert_batch(CandleInterval.FIVE_MINUTE, rows)
                touched_stock_ids = {row[0] for row in rows}
                for stock_id in touched_stock_ids:
                    self._coverage.refresh_stock_from_table(
                        stock_id,
                        CandleInterval.FIVE_MINUTE.value,
                    )
            stats["minute_edge_bars_rebuilt"] = len(rows)
            stats["minute_edge_groups_incomplete"] = len(bars) - len(rows)
        except Exception as exc:
            stats["minute_edge_errors"] = 1
            logger.warning("Could not rebuild five-minute edges from one-minute data: {}", exc)

        logger.info(
            "One-minute edge aggregation: rebuilt {}/{} missing five-minute bars; "
            "{} groups lacked all five source candles ({} fetched, {} rebuilt)",
            stats["minute_edge_bars_rebuilt"],
            stats["minute_edge_groups_requested"],
            stats["minute_edge_groups_incomplete"],
            stats["minute_edge_provider_groups_attempted"],
            stats["minute_edge_provider_bars_rebuilt"],
        )
        return stats

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
                    exchange=Exchange(exchange) if isinstance(exchange, str) else exchange,
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
