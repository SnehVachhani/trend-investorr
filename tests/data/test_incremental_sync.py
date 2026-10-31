"""Tests for incremental pending-candle synchronization."""

from datetime import datetime, timedelta

import pytest

from core.constants import CandleInterval, Exchange
from core.constants import IST
from core.exceptions import DataFetchError
from data.data_ingestion import DataIngestionService
from data.models.market_data import Candle


@pytest.mark.parametrize(
    ("last_ts", "interval", "days", "expected"),
    [
        (
            datetime(2026, 5, 15, 15, 25),
            CandleInterval.FIVE_MINUTE,
            90,
            datetime(2026, 6, 17, 18, 0),
        ),
        (
            datetime(2026, 9, 15, 15, 0),
            CandleInterval.FIVE_MINUTE,
            90,
            datetime(2026, 9, 15, 15, 5),
        ),
        (
            None,
            CandleInterval.ONE_DAY,
            365,
            datetime(2025, 9, 15, 18, 0),
        ),
    ],
)
def test_incremental_start_is_limited_to_pending_window(
    last_ts, interval, days, expected
):
    end = datetime(2026, 9, 15, 18, 0)

    start = DataIngestionService._get_incremental_start(
        last_ts=last_ts,
        end=end,
        interval=interval,
        max_history_days=days,
    )

    assert start == expected


def test_pending_sync_processes_api_intervals_and_aggregates_weekly(monkeypatch):
    service = DataIngestionService(
        historical_fetcher=None,
        candle_store=None,
        stock_store=None,
    )
    calls = []

    def fake_incremental_update(**kwargs):
        calls.append(kwargs)
        return {"candles_added": 1}

    monkeypatch.setattr(service, "incremental_update", fake_incremental_update)
    monkeypatch.setattr(service, "aggregate_weekly", lambda exchange: 7)

    stats = service.sync_pending_all_intervals()

    assert len(calls) == 5
    assert {call["interval"] for call in calls} == {
        CandleInterval.ONE_MINUTE,
        CandleInterval.FIVE_MINUTE,
        CandleInterval.FIFTEEN_MINUTE,
        CandleInterval.ONE_HOUR,
        CandleInterval.ONE_DAY,
    }
    assert all(call["max_history_days"] is not None for call in calls)
    assert stats[CandleInterval.ONE_WEEK.value]["candles_stored"] == 7


@pytest.mark.parametrize(
    ("last_ts", "end", "interval", "expected"),
    [
        (
            datetime(2026, 9, 15, 15, 29),
            datetime(2026, 9, 15, 18, 0),
            CandleInterval.ONE_MINUTE,
            True,
        ),
        (
            datetime(2026, 9, 15, 15, 25),
            datetime(2026, 9, 15, 18, 0),
            CandleInterval.FIVE_MINUTE,
            True,
        ),
        (
            datetime(2026, 9, 15, 15, 20),
            datetime(2026, 9, 15, 18, 0),
            CandleInterval.FIVE_MINUTE,
            False,
        ),
        (
            datetime(2026, 9, 15, 15, 29),
            datetime(2026, 9, 15, 14, 0),
            CandleInterval.ONE_MINUTE,
            False,
        ),
    ],
)
def test_interval_current_check_avoids_after_hours_noop_fetches(
    last_ts, end, interval, expected
):
    assert (
        DataIngestionService._is_interval_current(last_ts, end, interval)
        is expected
    )


class EmptyStockStore:
    def get_all_active(self, exchange="NSE"):
        return []


class CoverageRecorder:
    def __init__(self):
        self.refreshed_intervals = []

    def refresh_from_table(self, interval):
        self.refreshed_intervals.append(interval)


def test_daily_aggregation_refreshes_daily_coverage(monkeypatch):
    coverage = CoverageRecorder()
    service = DataIngestionService(
        historical_fetcher=None,
        candle_store=None,
        stock_store=EmptyStockStore(),
        coverage_store=coverage,
    )

    service.aggregate_all_daily(days=1)

    assert coverage.refreshed_intervals == [CandleInterval.ONE_DAY.value]


def test_weekly_aggregation_refreshes_weekly_coverage():
    coverage = CoverageRecorder()
    service = DataIngestionService(
        historical_fetcher=None,
        candle_store=None,
        stock_store=EmptyStockStore(),
        coverage_store=coverage,
    )

    service.aggregate_weekly()

    assert coverage.refreshed_intervals == [CandleInterval.ONE_WEEK.value]


def test_full_backfill_reconciles_coverage_before_each_api_interval(monkeypatch):
    coverage = CoverageRecorder()
    service = DataIngestionService(
        historical_fetcher=None,
        candle_store=None,
        stock_store=EmptyStockStore(),
        coverage_store=coverage,
    )

    backfilled = []
    monkeypatch.setattr(
        service,
        "backfill",
        lambda **kwargs: backfilled.append(kwargs["interval"]) or {},
    )
    monkeypatch.setattr(service, "aggregate_weekly", lambda exchange: 0)

    service.backfill_all_intervals()

    assert coverage.refreshed_intervals == [
        CandleInterval.ONE_MINUTE.value,
        CandleInterval.FIVE_MINUTE.value,
        CandleInterval.FIFTEEN_MINUTE.value,
        CandleInterval.ONE_HOUR.value,
        CandleInterval.ONE_DAY.value,
    ]
    assert backfilled == [
        CandleInterval.ONE_MINUTE,
        CandleInterval.FIVE_MINUTE,
        CandleInterval.FIFTEEN_MINUTE,
        CandleInterval.ONE_HOUR,
        CandleInterval.ONE_DAY,
    ]


class _BackfillStockStore:
    def __init__(self):
        self.stock = {"id": 1, "symbol": "TEST-EQ", "token": "1"}

    def get_all_active(self, exchange="NSE"):
        return [self.stock]


class _BackfillCandleStore:
    def __init__(self):
        self.rows = []

    def insert_batch(self, interval, rows):
        self.rows.extend(rows)
        return len(rows)


class _BackfillCoverage:
    def __init__(self):
        self.coverage = None
        self.check = None

    def get(self, stock_id, interval):
        return self.coverage

    def upsert(self, **kwargs):
        self.coverage = {
            "earliest_ts": kwargs["earliest"],
            "latest_ts": kwargs["latest"],
            "history_complete": kwargs.get("history_complete") is True,
        }

    def record_check(self, stock_id, interval, result, error=""):
        self.check = {
            "last_checked_at": datetime.now(IST),
            "last_result": result,
            "last_error": error,
        }

    def mark_history_complete(self, stock_id, interval):
        if self.coverage:
            self.coverage["history_complete"] = True

    def get_last_check(self, stock_id, interval):
        return self.check


def _make_backfill_service(fetcher, coverage=None):
    return DataIngestionService(
        historical_fetcher=fetcher,
        candle_store=_BackfillCandleStore(),
        stock_store=_BackfillStockStore(),
        coverage_store=coverage or _BackfillCoverage(),
    )


def test_on_demand_backfill_uses_the_stock_exchange(monkeypatch):
    class ExchangeCapturingFetcher:
        def __init__(self):
            self.calls = []

        def get_candles(self, **kwargs):
            self.calls.append(kwargs)
            return []

    fetcher = ExchangeCapturingFetcher()
    service = _make_backfill_service(fetcher)
    monkeypatch.setattr("data.data_ingestion.time.sleep", lambda _: None)

    service.backfill_stock(
        stock={"id": 1, "symbol": "MAHALIFE", "token": "544611", "exchange": "BSE"},
        intervals=[CandleInterval.ONE_DAY],
    )

    assert fetcher.calls[0]["exchange"] is Exchange.BSE


def test_backfill_retries_failed_stocks_and_records_success(monkeypatch):
    class RetryFetcher:
        def __init__(self):
            self.calls = 0

        def get_candles(self, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise DataFetchError("temporary failure")
            return [
                Candle(
                    symbol="TEST-EQ",
                    timestamp=datetime.now(IST) - timedelta(minutes=5),
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    volume=10,
                    interval=CandleInterval.FIVE_MINUTE,
                )
            ]

    fetcher = RetryFetcher()
    coverage = _BackfillCoverage()
    service = _make_backfill_service(fetcher, coverage)
    monkeypatch.setattr("data.data_ingestion.time.sleep", lambda _: None)

    stats = service.backfill(
        days=90,
        interval=CandleInterval.FIVE_MINUTE,
        max_workers=1,
        max_stock_retries=2,
        aggregate_daily=False,
        batch_pause=0,
    )

    assert fetcher.calls == 3
    assert stats["stocks_processed"] == 1
    assert stats["retry_attempts"] == 2
    assert stats["errors"] == 0
    assert coverage.check["last_result"] == "data"


def test_backfill_fetches_only_the_missing_older_range(monkeypatch):
    class RangeFetcher:
        def __init__(self):
            self.calls = []

        def get_candles_range(self, **kwargs):
            self.calls.append(kwargs)
            return [
                Candle(
                    symbol="TEST-EQ",
                    timestamp=kwargs["start"],
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    volume=10,
                    interval=CandleInterval.FIVE_MINUTE,
                )
            ]

    fetcher = RangeFetcher()
    coverage = _BackfillCoverage()
    existing_now = datetime.now(IST)
    original_earliest = existing_now - timedelta(days=30)
    coverage.coverage = {
        "earliest_ts": original_earliest,
        "latest_ts": existing_now + timedelta(minutes=1),
        "history_complete": False,
    }
    service = _make_backfill_service(fetcher, coverage)

    stats = service.backfill(
        days=90,
        interval=CandleInterval.FIVE_MINUTE,
        max_workers=1,
        max_stock_retries=0,
        aggregate_daily=False,
        batch_pause=0,
    )

    assert len(fetcher.calls) == 1
    requested = fetcher.calls[0]
    assert requested["start"] < original_earliest
    assert requested["end"] < original_earliest
    assert requested["end"] < requested["start"] + timedelta(days=61)
    assert stats["stocks_processed"] == 1
    assert stats["errors"] == 0


def test_empty_market_response_is_not_counted_as_failure_or_retried(
    monkeypatch,
):
    class EmptyFetcher:
        def __init__(self):
            self.calls = 0

        def get_candles(self, **kwargs):
            self.calls += 1
            return []

    fetcher = EmptyFetcher()
    coverage = _BackfillCoverage()
    service = _make_backfill_service(fetcher, coverage)
    monkeypatch.setattr("data.data_ingestion.time.sleep", lambda _: None)

    first = service.backfill(
        days=90,
        interval=CandleInterval.FIVE_MINUTE,
        max_workers=1,
        max_stock_retries=2,
        aggregate_daily=False,
        batch_pause=0,
    )
    second = service.backfill(
        days=90,
        interval=CandleInterval.FIVE_MINUTE,
        max_workers=1,
        max_stock_retries=2,
        aggregate_daily=False,
        batch_pause=0,
    )

    assert first["no_data"] == 1
    assert first["errors"] == 0
    assert second["no_data"] == 1
    assert second["errors"] == 0
    assert fetcher.calls == 1
