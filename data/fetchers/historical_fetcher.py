"""
Trend-Investorr: Historical Data Fetcher

Fetches OHLCV candle data from Angel One SmartAPI for backtesting,
indicator calculation, and volume average computation.

SmartAPI limits:
    - Max 2000 candles per request
    - Max 3 requests/second, 180/minute, and 5000/hour per client code
    - Historical data available for ~2 years

Raises:
    DataFetchError: If the API call fails after retries.
    RateLimitError: If API rate limits are exceeded.
    InstrumentNotFoundError: If symbol/token is invalid.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from loguru import logger

from core.constants import CandleInterval, Exchange, IST
from core.exceptions import (
    DataFetchError,
    RateLimitError,
    InstrumentNotFoundError,
    SessionExpiredError,
)
from data.ohlcv import is_valid_ohlcv
from data.models.market_data import Candle


# Map our interval enum to SmartAPI interval strings
# Note: ONE_WEEK is NOT an API interval — weekly candles are aggregated from daily
_INTERVAL_MAP = {
    CandleInterval.ONE_MINUTE: "ONE_MINUTE",
    CandleInterval.FIVE_MINUTE: "FIVE_MINUTE",
    CandleInterval.FIFTEEN_MINUTE: "FIFTEEN_MINUTE",
    CandleInterval.ONE_HOUR: "ONE_HOUR",
    CandleInterval.ONE_DAY: "ONE_DAY",
}

# SmartAPI datetime format
_DATETIME_FMT = "%Y-%m-%d %H:%M"


class HistoricalFetcher:
    """
    Fetches historical candle data from SmartAPI.

    Usage:
        fetcher = HistoricalFetcher(session=smartapi_session)
        candles = fetcher.get_candles("RELIANCE", "2885", days=30)
        candles = fetcher.get_candles_range("RELIANCE", "2885", start, end)
    """

    # SmartAPI limit per request
    MAX_CANDLES_PER_REQUEST = 2000
    # The hourly provider quota is the binding limit for long backfills:
    # 5000 requests/hour = 0.72 seconds/request.  This is intentionally
    # slower than the per-second limit so a sustained sync does not exhaust
    # the hourly quota and get trapped in repeated rate-limit retries.
    MIN_REQUEST_INTERVAL = 0.72

    def __init__(self, session: Any):
        """
        Args:
            session: SmartAPISession instance for authenticated API calls.
        """
        self._session = session
        self._rate_limit_lock = Lock()
        self._next_request_at = 0.0

    def _wait_for_rate_limit(self) -> None:
        """Serialize historical request starts across concurrent workers."""
        with self._rate_limit_lock:
            now = time.monotonic()
            delay = self._next_request_at - now
            if delay > 0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + self.MIN_REQUEST_INTERVAL

    def _defer_after_rate_limit(self, retry_after: float) -> None:
        """Pause all workers after the provider reports a rate limit."""
        with self._rate_limit_lock:
            self._next_request_at = max(
                self._next_request_at,
                time.monotonic() + retry_after,
            )

    def get_candles(
        self,
        symbol: str,
        token: str,
        days: int = 30,
        interval: CandleInterval = CandleInterval.ONE_DAY,
        exchange: Exchange = Exchange.NSE,
    ) -> list[Candle]:
        """
        Fetch the last N days of candle data.

        Args:
            symbol: Stock symbol (e.g., "RELIANCE").
            token: SmartAPI token (e.g., "2885").
            days: Number of days to look back.
            interval: Candle interval.
            exchange: Stock exchange.

        Returns:
            List of Candle dataclasses, sorted by timestamp ascending.

        Raises:
            DataFetchError: If the API call fails.
        """
        end = datetime.now(IST)
        start = end - timedelta(days=days)
        return self.get_candles_range(symbol, token, start, end, interval, exchange)

    def get_candles_range(
        self,
        symbol: str,
        token: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval = CandleInterval.ONE_DAY,
        exchange: Exchange = Exchange.NSE,
    ) -> list[Candle]:
        """
        Fetch candle data for a specific date range.

        Handles pagination automatically if the range exceeds
        SmartAPI's 2000-candle limit.

        Args:
            symbol: Stock symbol.
            token: SmartAPI token.
            start: Start datetime (inclusive).
            end: End datetime (inclusive).
            interval: Candle interval.
            exchange: Stock exchange.

        Returns:
            List of Candle dataclasses, sorted by timestamp ascending.

        Raises:
            DataFetchError: If the API call fails after retries.
        """
        all_candles: list[Candle] = []
        chunks: list[tuple[datetime, datetime]] = []
        current_start = start

        # Calculate chunk size based on interval
        chunk_days = self._get_chunk_days(interval)

        while current_start < end:
            current_end = min(current_start + timedelta(days=chunk_days), end)
            chunks.append((current_start, current_end))
            current_start = current_end + timedelta(minutes=1)

        failed_chunks: list[tuple[datetime, datetime, Exception]] = []

        def _fetch_chunks(
            pending: list[tuple[datetime, datetime]],
        ) -> list[tuple[datetime, datetime, Exception]]:
            failed: list[tuple[datetime, datetime, Exception]] = []
            for chunk_start, chunk_end in pending:
                try:
                    chunk = self._fetch_chunk(
                        symbol=symbol,
                        token=token,
                        start=chunk_start,
                        end=chunk_end,
                        interval=interval,
                        exchange=exchange,
                    )
                    all_candles.extend(chunk)
                except (DataFetchError, RateLimitError) as e:
                    failed.append((chunk_start, chunk_end, e))
                    logger.warning(
                        f"Failed to fetch chunk for {symbol} "
                        f"({chunk_start.date()} to {chunk_end.date()}): {e}"
                    )

                # Brief pause between chunks to respect rate limits.
                time.sleep(0.1)
            return failed

        failed_chunks = _fetch_chunks(chunks)

        # A network interruption can affect a whole range after the per-request
        # retries are exhausted. Retry only those failed chunks so a transient
        # outage does not permanently turn a stock into a partial backfill.
        for retry_round in range(1, 3):
            if not failed_chunks:
                break
            time.sleep(2 ** retry_round)
            failed_chunks = _fetch_chunks(
                [(chunk_start, chunk_end) for chunk_start, chunk_end, _ in failed_chunks]
            )

        if failed_chunks:
            # Do not return partial data: the caller would update coverage with
            # only the first/last successful candle and hide the missing range.
            raise failed_chunks[0][2]

        # Deduplicate by timestamp (overlapping chunks may have dupes)
        seen_timestamps: set[str] = set()
        unique_candles: list[Candle] = []
        for candle in all_candles:
            ts_key = f"{candle.symbol}_{candle.timestamp.isoformat()}"
            if ts_key not in seen_timestamps:
                seen_timestamps.add(ts_key)
                unique_candles.append(candle)

        # Sort by timestamp ascending
        unique_candles.sort(key=lambda c: c.timestamp)

        logger.info(
            f"Historical data for {symbol}: "
            f"{len(unique_candles)} candles ({interval.value}) | "
            f"{start.date()} to {end.date()}"
        )
        return unique_candles

    def _fetch_chunk(
        self,
        symbol: str,
        token: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
        exchange: Exchange,
    ) -> list[Candle]:
        """
        Fetch a single chunk of candle data from SmartAPI.

        Includes retry logic with exponential backoff.

        Raises:
            DataFetchError: If all retries fail.
        """
        smartapi_interval = _INTERVAL_MAP.get(interval)
        if not smartapi_interval:
            raise DataFetchError(
                f"Unsupported candle interval: {interval}",
                details={"interval": interval.value},
            )

        params = {
            "exchange": exchange.value,
            "symboltoken": token,
            "interval": smartapi_interval,
            "fromdate": start.strftime(_DATETIME_FMT),
            "todate": end.strftime(_DATETIME_FMT),
        }

        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                api = self._session.get_api()
                self._wait_for_rate_limit()
                response = api.getCandleData(params)

                if not response:
                    raise DataFetchError(
                        f"Empty response for {symbol} historical data",
                        details={"params": params},
                    )

                if response.get("status") is False:
                    error_msg = response.get("message", "Unknown error")

                    # Check for rate limit response
                    if "rate" in error_msg.lower() or "limit" in error_msg.lower():
                        raise RateLimitError(
                            f"Rate limited on historical data for {symbol}",
                            retry_after=2.0,
                            symbol=symbol,
                        )

                    # Check for invalid token/symbol
                    if "invalid" in error_msg.lower() or "not found" in error_msg.lower():
                        raise InstrumentNotFoundError(
                            f"{symbol} (token: {token})",
                            error=error_msg,
                        )

                    raise DataFetchError(
                        f"SmartAPI historical data error for {symbol}: {error_msg}",
                        details={"symbol": symbol, "params": params, "response": str(response)},
                    )

                # Parse candle data
                candle_data = response.get("data", [])
                if not candle_data:
                    return []  # No data for this period (e.g., holidays)

                return self._parse_candles(symbol, candle_data, interval)

            except (DataFetchError, InstrumentNotFoundError):
                raise  # Don't retry our own validated errors

            except RateLimitError as e:
                logger.warning(f"Rate limited for {symbol} — waiting {e.retry_after}s")
                self._defer_after_rate_limit(e.retry_after)
                time.sleep(e.retry_after)
                last_error = e

            except SessionExpiredError:
                raise  # Let caller handle re-auth

            except Exception as e:
                error_message = str(e)
                error_lower = error_message.lower()
                if (
                    "exceeding access rate" in error_lower
                    or ("rate" in error_lower and "limit" in error_lower)
                    or "too many request" in error_lower
                ):
                    last_error = RateLimitError(
                        f"Rate limited on historical data for {symbol}",
                        retry_after=2.0,
                        symbol=symbol,
                    )
                    logger.warning(
                        f"Rate limited for {symbol} — waiting {last_error.retry_after}s"
                    )
                    self._defer_after_rate_limit(last_error.retry_after)
                    if attempt < max_retries:
                        time.sleep(last_error.retry_after)
                    continue

                # SDK/HTTP exception strings can include request headers or
                # authenticated URLs. Keep the failure class for diagnostics,
                # but never persist or log the raw provider exception text.
                last_error = DataFetchError(
                    f"Historical fetch error for {symbol} ({type(e).__name__})",
                    details={
                        "symbol": symbol,
                        "attempt": attempt,
                        "error": type(e).__name__,
                    },
                )
                logger.warning(
                    "Historical fetch failed for {} (attempt {}/{}): {}",
                    symbol,
                    attempt,
                    max_retries,
                    type(e).__name__,
                )

                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)

        raise last_error or DataFetchError(f"Historical data fetch failed for {symbol}")

    def _parse_candles(
        self,
        symbol: str,
        raw_data: list[list],
        interval: CandleInterval,
    ) -> list[Candle]:
        """
        Parse SmartAPI candle response into Candle dataclasses.

        SmartAPI format: [[timestamp, open, high, low, close, volume], ...]
        """
        candles: list[Candle] = []
        invalid_count = 0

        for row in raw_data:
            try:
                if not row or len(row) < 6:
                    invalid_count += 1
                    continue

                timestamp = datetime.strptime(str(row[0]), "%Y-%m-%dT%H:%M:%S%z")
                open_ = float(row[1])
                high = float(row[2])
                low = float(row[3])
                close = float(row[4])
                volume = int(row[5])
                if not is_valid_ohlcv(open_, high, low, close, volume):
                    invalid_count += 1
                    continue
                candle = Candle(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    interval=interval,
                )
                candles.append(candle)

            except (ValueError, IndexError, TypeError, OverflowError) as e:
                # Skip malformed candle data, log at debug level
                invalid_count += 1
                logger.debug(f"Skipping malformed candle for {symbol}: {row} — {e}")
                continue

        if invalid_count:
            logger.warning(f"Skipped {invalid_count} invalid OHLCV candle(s) for {symbol}")

        return candles

    @staticmethod
    def _get_chunk_days(interval: CandleInterval) -> int:
        """
        Max days per request as per Angel One SmartAPI documentation.

        Source: https://smartapi.angelone.in/docs/CandleData
            ONE_MINUTE      → 30 days
            FIVE_MINUTE     → 100 days
            FIFTEEN_MINUTE  → 200 days
            ONE_HOUR        → 400 days
            ONE_DAY         → 2000 days
        """
        match interval:
            case CandleInterval.ONE_MINUTE:
                return 30
            case CandleInterval.FIVE_MINUTE:
                return 100
            case CandleInterval.FIFTEEN_MINUTE:
                return 200
            case CandleInterval.ONE_HOUR:
                return 400
            case CandleInterval.ONE_DAY:
                return 2000
            case _:
                return 30

    # ── Convenience Methods ──────────────────────────────

    def get_daily_candles(self, symbol: str, token: str, days: int = 90) -> list[Candle]:
        """Shortcut: fetch daily candles."""
        return self.get_candles(symbol, token, days=days, interval=CandleInterval.ONE_DAY)

    def get_intraday_candles(
        self,
        symbol: str,
        token: str,
        days: int = 5,
        interval: CandleInterval = CandleInterval.FIVE_MINUTE,
    ) -> list[Candle]:
        """Shortcut: fetch intraday candles."""
        return self.get_candles(symbol, token, days=days, interval=interval)

    def get_volume_history(self, symbol: str, token: str, days: int = 90) -> list[int]:
        """
        Get daily volume history for average calculation.

        Returns:
            List of daily volumes sorted ascending by date.
        """
        candles = self.get_daily_candles(symbol, token, days=days)
        return [c.volume for c in candles]

    def get_latest_price(self, symbol: str, token: str) -> float | None:
        """
        Get the most recent closing price.

        Returns:
            Latest close price, or None if no data available.
        """
        try:
            candles = self.get_candles(
                symbol, token,
                days=5,
                interval=CandleInterval.ONE_DAY,
            )
            if candles:
                return candles[-1].close
            return None
        except DataFetchError as e:
            logger.warning(f"Could not fetch latest price for {symbol}: {e}")
            return None
