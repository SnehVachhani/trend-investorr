"""Tests for SmartAPI historical request pacing and rate-limit handling."""

from datetime import datetime

import pytest

import data.fetchers.historical_fetcher as historical_fetcher
from core.constants import CandleInterval, Exchange
from core.exceptions import DataFetchError, RateLimitError
from data.fetchers.historical_fetcher import HistoricalFetcher
from data.models.market_data import Candle


class FakeRateLimitedApi:
    def __init__(self, message: str):
        self.message = message
        self.calls = 0

    def getCandleData(self, params):
        self.calls += 1
        raise RuntimeError(self.message)


class FakeSession:
    def __init__(self, api):
        self.api = api

    def get_api(self):
        return self.api


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_historical_requests_are_spaced_to_avoid_bursts(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(historical_fetcher.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(historical_fetcher.time, "sleep", clock.sleep)
    fetcher = HistoricalFetcher(session=None)

    fetcher._wait_for_rate_limit()
    fetcher._wait_for_rate_limit()

    assert fetcher.MIN_REQUEST_INTERVAL == pytest.approx(0.72)
    assert clock.sleeps == [pytest.approx(fetcher.MIN_REQUEST_INTERVAL)]


def test_rate_limit_backoff_delays_the_next_shared_request(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(historical_fetcher.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(historical_fetcher.time, "sleep", clock.sleep)
    fetcher = HistoricalFetcher(session=None)

    fetcher._wait_for_rate_limit()
    fetcher._defer_after_rate_limit(2.0)
    fetcher._wait_for_rate_limit()

    assert clock.sleeps == [pytest.approx(2.0)]


def test_transport_rate_limit_is_reported_as_rate_limit_error(monkeypatch):
    api = FakeRateLimitedApi("Access denied because of exceeding access rate")
    fetcher = HistoricalFetcher(session=FakeSession(api))
    monkeypatch.setattr(historical_fetcher.time, "sleep", lambda _: None)

    with pytest.raises(RateLimitError):
        fetcher._fetch_chunk(
            symbol="TEST-EQ",
            token="1",
            start=datetime(2026, 9, 1),
            end=datetime(2026, 9, 2),
            interval=CandleInterval.ONE_DAY,
            exchange=Exchange.NSE,
        )

    assert api.calls == 3


def test_range_sync_does_not_hide_when_every_chunk_fails(monkeypatch):
    api = FakeRateLimitedApi("Access denied because of exceeding access rate")
    fetcher = HistoricalFetcher(session=FakeSession(api))
    monkeypatch.setattr(historical_fetcher.time, "sleep", lambda _: None)

    with pytest.raises(RateLimitError):
        fetcher.get_candles_range(
            symbol="TEST-EQ",
            token="1",
            start=datetime(2026, 9, 1),
            end=datetime(2026, 9, 2),
            interval=CandleInterval.ONE_DAY,
            exchange=Exchange.NSE,
        )

    # The chunk-level retry (3 attempts) is followed by two range-level
    # retries for a transient broker rate-limit response.
    assert api.calls == 9


def test_range_retries_a_failed_chunk_before_returning(monkeypatch):
    fetcher = HistoricalFetcher(session=None)
    calls = 0

    def fake_fetch_chunk(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DataFetchError("temporary network failure")
        return [
            Candle(
                symbol="TEST-EQ",
                timestamp=datetime(2026, 9, 1, 10, 0),
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=10,
                interval=CandleInterval.ONE_DAY,
            )
        ]

    monkeypatch.setattr(fetcher, "_fetch_chunk", fake_fetch_chunk)
    monkeypatch.setattr(historical_fetcher.time, "sleep", lambda _: None)

    candles = fetcher.get_candles_range(
        symbol="TEST-EQ",
        token="1",
        start=datetime(2026, 9, 1),
        end=datetime(2026, 9, 2),
        interval=CandleInterval.ONE_DAY,
        exchange=Exchange.NSE,
    )

    assert len(candles) == 1
    assert calls == 2


def test_range_does_not_return_partial_data_after_failed_chunk_retries(monkeypatch):
    fetcher = HistoricalFetcher(session=None)
    calls = 0

    def fake_fetch_chunk(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                Candle(
                    symbol="TEST-EQ",
                    timestamp=datetime(2020, 1, 1, 10, 0),
                    open=100,
                    high=101,
                    low=99,
                    close=100.5,
                    volume=10,
                    interval=CandleInterval.ONE_DAY,
                )
            ]
        raise DataFetchError("persistent network failure")

    monkeypatch.setattr(fetcher, "_fetch_chunk", fake_fetch_chunk)
    monkeypatch.setattr(historical_fetcher.time, "sleep", lambda _: None)

    with pytest.raises(DataFetchError):
        fetcher.get_candles_range(
            symbol="TEST-EQ",
            token="1",
            start=datetime(2020, 1, 1),
            end=datetime(2026, 1, 1),
            interval=CandleInterval.ONE_DAY,
            exchange=Exchange.NSE,
        )

    assert calls == 4


def test_historical_parser_skips_non_ohlcv_provider_rows():
    fetcher = HistoricalFetcher(session=None)
    rows = fetcher._parse_candles(
        "TEST-EQ",
        [
            ["2026-09-18T09:15:00+05:30", 100, 102, 99, 101, 50],
            ["2026-09-18T09:20:00+05:30", 100, 100, 100, 100, 0],
            ["2026-09-18T09:25:00+05:30", 100, 102, 99, 101, -12],
            ["2026-09-18T09:30:00+05:30", 100, 99, 98, 101, 50],
            ["2026-09-18T09:35:00+05:30", 100, 102, 101, 99, 50],
            ["2026-09-18T09:40:00+05:30", "NaN", 102, 99, 101, 50],
        ],
        CandleInterval.FIVE_MINUTE,
    )

    assert [candle.timestamp.minute for candle in rows] == [15, 20]
    assert [candle.volume for candle in rows] == [50, 0]
