"""
Trend-Investorr: NSE Bhavcopy (EOD Data) Fetcher

Downloads the NSE daily bhavcopy — the official End-Of-Day data file
containing OHLCV + delivery data for ALL listed securities.

Bhavcopy is used for:
    - Post-market analysis (sector performance, volume leaders)
    - Recalculating 30/90-day volume averages
    - Delivery percentage analysis (institutional interest proxy)
    - Backfilling any missed intraday data

NSE publishes bhavcopy after market close (~6 PM IST) daily.

Raises:
    DataFetchError: If download fails after retries and fallbacks.
"""

from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from loguru import logger

from core.constants import IST, NSE_REQUIRED_HEADERS
from core.exceptions import DataFetchError
from data.models.market_data import Candle, CandleInterval


class BhavcopyFetcher:
    """
    Downloads and parses NSE daily bhavcopy files.

    Supports multiple URL formats (NSE changes URLs periodically)
    and falls back to T-1/T-2 dates if today's bhavcopy isn't available yet.

    Usage:
        fetcher = BhavcopyFetcher(cache_dir=Path("data_cache/bhavcopy"))
        candles = fetcher.fetch()           # Today's bhavcopy
        candles = fetcher.fetch(date_obj)   # Specific date
    """

    # Multiple URL templates — NSE occasionally changes formats
    URL_TEMPLATES = [
        # New format (2024+)
        "https://nsearchives.nseindia.com/content/cm/BhsecQryData_{date_ddmmyyyy}.csv",
        # Older format
        "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_ddmmyyyy}.csv",
        # Alternative format
        "https://www1.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{date_ddMONyyyy}bhav.csv.zip",
    ]

    def __init__(self, cache_dir: Path):
        """
        Args:
            cache_dir: Directory to cache downloaded bhavcopy files.
        """
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update(NSE_REQUIRED_HEADERS)

    def fetch(
        self,
        target_date: datetime | None = None,
        max_fallback_days: int = 3,
    ) -> list[Candle]:
        """
        Fetch bhavcopy for a target date.

        Falls back to previous trading days if the target date's
        bhavcopy is not available (weekends, holidays, not yet published).

        Args:
            target_date: Date to fetch. Defaults to today.
            max_fallback_days: Max days to go back if today's isn't available.

        Returns:
            List of Candle dataclasses (one per stock, daily interval).

        Raises:
            DataFetchError: If no bhavcopy is found within the fallback window.
        """
        if target_date is None:
            target_date = datetime.now(IST)

        # Try target date, then fall back
        for days_back in range(max_fallback_days + 1):
            check_date = target_date - timedelta(days=days_back)

            # Skip weekends
            if check_date.weekday() >= 5:
                continue

            try:
                # Try cache first
                cached = self._load_from_cache(check_date)
                if cached:
                    logger.info(
                        f"Bhavcopy loaded from cache: {check_date.strftime('%Y-%m-%d')} | "
                        f"{len(cached)} stocks"
                    )
                    return cached

                # Download fresh
                candles = self._download(check_date)
                if candles:
                    logger.info(
                        f"Bhavcopy downloaded: {check_date.strftime('%Y-%m-%d')} | "
                        f"{len(candles)} stocks"
                    )
                    return candles

            except DataFetchError as e:
                if days_back < max_fallback_days:
                    logger.debug(
                        f"Bhavcopy not available for {check_date.strftime('%Y-%m-%d')} "
                        f"— trying T-{days_back + 1}"
                    )
                    continue
                raise

        raise DataFetchError(
            f"No bhavcopy available within {max_fallback_days} days of "
            f"{target_date.strftime('%Y-%m-%d')}",
            details={"target_date": target_date.strftime("%Y-%m-%d"), "fallback_days": max_fallback_days},
        )

    def _download(self, target_date: datetime) -> list[Candle]:
        """
        Try downloading bhavcopy from multiple URL formats.

        Raises:
            DataFetchError: If all URL templates fail.
        """
        date_str_ddmmyyyy = target_date.strftime("%d%m%Y")
        date_str_ddMONyyyy = target_date.strftime("%d%b%Y").upper()
        year = target_date.strftime("%Y")
        month = target_date.strftime("%b").upper()

        last_error: Exception | None = None

        for template in self.URL_TEMPLATES:
            url = template.format(
                date_ddmmyyyy=date_str_ddmmyyyy,
                date_ddMONyyyy=date_str_ddMONyyyy,
                year=year,
                month=month,
            )

            try:
                logger.debug(f"Trying bhavcopy URL: {url}")

                # NSE requires cookies — get them first
                try:
                    self._session.get("https://www.nseindia.com", timeout=10)
                except Exception:
                    pass  # Cookie fetch failure is non-fatal

                response = self._session.get(url, timeout=30)

                if response.status_code == 404:
                    continue  # Try next URL format

                response.raise_for_status()

                content = response.content

                # Handle ZIP files
                if url.endswith(".zip"):
                    content = self._extract_zip(content)

                # Parse CSV
                candles = self._parse_bhavcopy(content, target_date)

                if candles:
                    # Cache for future use
                    self._save_to_cache(target_date, content)
                    return candles

            except DataFetchError:
                raise

            except requests.exceptions.Timeout:
                last_error = DataFetchError(
                    f"Bhavcopy download timed out for {target_date.strftime('%Y-%m-%d')}",
                    details={"url": url},
                )
                logger.debug(f"Timeout on {url}")

            except requests.exceptions.RequestException as e:
                last_error = DataFetchError(
                    f"Bhavcopy download failed: {e}",
                    details={"url": url, "error": str(e)},
                )
                logger.debug(f"Request error on {url}: {e}")

            except Exception as e:
                last_error = DataFetchError(
                    f"Unexpected error downloading bhavcopy: {e}",
                    details={"url": url, "error": type(e).__name__},
                )
                logger.debug(f"Unexpected error on {url}: {e}")

            # Brief pause between URL attempts
            time.sleep(0.5)

        raise last_error or DataFetchError(
            f"All bhavcopy URL formats failed for {target_date.strftime('%Y-%m-%d')}"
        )

    def _parse_bhavcopy(self, content: bytes, target_date: datetime) -> list[Candle]:
        """
        Parse bhavcopy CSV content into Candle dataclasses.

        Handles multiple CSV formats (column name variations).
        """
        try:
            # Try different encodings
            try:
                df = pd.read_csv(io.BytesIO(content), encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(io.BytesIO(content), encoding="latin-1")

            if df.empty:
                return []

            # Normalize column names (strip whitespace, lowercase)
            df.columns = df.columns.str.strip().str.upper()

            # Map various column names to standard names
            col_map = self._detect_columns(df)

            if not col_map.get("symbol") or not col_map.get("close"):
                logger.warning(f"Bhavcopy columns not recognized: {list(df.columns)}")
                return []

            candles: list[Candle] = []

            for _, row in df.iterrows():
                try:
                    symbol = str(row[col_map["symbol"]]).strip()
                    series = str(row.get(col_map.get("series", ""), "EQ")).strip()

                    # Only process EQ series (equity)
                    if series not in ("EQ", "BE", "BZ"):
                        continue

                    open_price = float(row.get(col_map.get("open", ""), 0) or 0)
                    high = float(row.get(col_map.get("high", ""), 0) or 0)
                    low = float(row.get(col_map.get("low", ""), 0) or 0)
                    close = float(row[col_map["close"]])
                    volume = int(float(row.get(col_map.get("volume", ""), 0) or 0))

                    # Delivery data (if available)
                    delivery_qty = int(float(row.get(col_map.get("delivery_qty", ""), 0) or 0))
                    delivery_pct = float(row.get(col_map.get("delivery_pct", ""), 0) or 0)

                    if close <= 0:
                        continue

                    candle = Candle(
                        symbol=symbol,
                        timestamp=target_date.replace(hour=15, minute=30),
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        interval=CandleInterval.ONE_DAY,
                        delivery_qty=delivery_qty,
                        delivery_pct=delivery_pct,
                    )
                    candles.append(candle)

                except (ValueError, KeyError, TypeError) as e:
                    continue  # Skip malformed rows

            return candles

        except pd.errors.EmptyDataError:
            return []

        except Exception as e:
            raise DataFetchError(
                f"Failed to parse bhavcopy: {e}",
                details={"error": type(e).__name__, "message": str(e)},
            )

    @staticmethod
    def _detect_columns(df: pd.DataFrame) -> dict[str, str]:
        """
        Detect bhavcopy column names across different formats.

        NSE has changed the CSV format multiple times,
        so we support various column naming conventions.
        """
        columns = set(df.columns)
        col_map: dict[str, str] = {}

        # Symbol
        for candidate in ["SYMBOL", "TckrSymb", "TCKRSYMB"]:
            if candidate in columns:
                col_map["symbol"] = candidate
                break

        # Series
        for candidate in ["SERIES", "SctySrs", "SCTYSRS"]:
            if candidate in columns:
                col_map["series"] = candidate
                break

        # OHLCV
        for candidate in ["OPEN_PRICE", "OPEN", "OpnPric", "OPNPRIC"]:
            if candidate in columns:
                col_map["open"] = candidate
                break

        for candidate in ["HIGH_PRICE", "HIGH", "HghPric", "HGHPRIC"]:
            if candidate in columns:
                col_map["high"] = candidate
                break

        for candidate in ["LOW_PRICE", "LOW", "LwPric", "LWPRIC"]:
            if candidate in columns:
                col_map["low"] = candidate
                break

        for candidate in ["CLOSE_PRICE", "CLOSE", "ClsPric", "CLSPRIC"]:
            if candidate in columns:
                col_map["close"] = candidate
                break

        for candidate in ["TTL_TRD_QNTY", "TOTTRDQTY", "TtlTrdQnty", "VOLUME"]:
            if candidate in columns:
                col_map["volume"] = candidate
                break

        # Delivery
        for candidate in ["DELIV_QTY", "DlvryQty", "DELVRY_QTY"]:
            if candidate in columns:
                col_map["delivery_qty"] = candidate
                break

        for candidate in ["DELIV_PER", "DlvryPct", "PCT_DELVRY_QTY"]:
            if candidate in columns:
                col_map["delivery_pct"] = candidate
                break

        return col_map

    @staticmethod
    def _extract_zip(content: bytes) -> bytes:
        """Extract CSV from a ZIP archive.

        Raises:
            DataFetchError: If ZIP is invalid or contains no CSV.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_files = [f for f in zf.namelist() if f.endswith(".csv")]
                if not csv_files:
                    raise DataFetchError(
                        "Bhavcopy ZIP contains no CSV files",
                        details={"files_in_zip": zf.namelist()},
                    )
                return zf.read(csv_files[0])
        except zipfile.BadZipFile:
            raise DataFetchError("Bhavcopy download is not a valid ZIP file")

    # ── Cache ────────────────────────────────────────────

    def _cache_path(self, target_date: datetime) -> Path:
        """Get cache file path for a date."""
        return self._cache_dir / f"bhavcopy_{target_date.strftime('%Y-%m-%d')}.csv"

    def _save_to_cache(self, target_date: datetime, content: bytes) -> None:
        """Save bhavcopy CSV to cache."""
        try:
            cache_file = self._cache_path(target_date)
            cache_file.write_bytes(content)
            logger.debug(f"Bhavcopy cached: {cache_file.name}")
        except OSError as e:
            logger.warning(f"Failed to cache bhavcopy (non-critical): {e}")

    def _load_from_cache(self, target_date: datetime) -> list[Candle] | None:
        """Load bhavcopy from cache if available."""
        cache_file = self._cache_path(target_date)
        if not cache_file.exists():
            return None

        try:
            content = cache_file.read_bytes()
            return self._parse_bhavcopy(content, target_date)
        except Exception as e:
            logger.debug(f"Cache load failed for {cache_file.name}: {e}")
            return None

    def cleanup_old_cache(self, keep_days: int = 90) -> int:
        """
        Delete cached bhavcopy files older than keep_days.

        Returns:
            Number of files deleted.
        """
        cutoff = datetime.now(IST) - timedelta(days=keep_days)
        deleted = 0

        try:
            for cache_file in self._cache_dir.glob("bhavcopy_*.csv"):
                try:
                    # Extract date from filename
                    date_str = cache_file.stem.replace("bhavcopy_", "")
                    file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST.tzinfo if hasattr(IST, 'tzinfo') else None)

                    if file_date < cutoff:
                        cache_file.unlink()
                        deleted += 1
                except (ValueError, OSError):
                    continue
        except OSError as e:
            logger.warning(f"Error during cache cleanup: {e}")

        if deleted:
            logger.info(f"Cleaned up {deleted} old bhavcopy cache files")

        return deleted

    # ── Convenience Methods ──────────────────────────────

    def get_delivery_leaders(
        self,
        target_date: datetime | None = None,
        min_delivery_pct: float = 50.0,
        min_volume: int = 100_000,
    ) -> list[Candle]:
        """
        Get stocks with high delivery percentage (institutional interest).

        Args:
            target_date: Date to check. Defaults to latest available.
            min_delivery_pct: Minimum delivery percentage (0-100).
            min_volume: Minimum traded volume.

        Returns:
            List of Candle dataclasses sorted by delivery % descending.
        """
        candles = self.fetch(target_date)
        leaders = [
            c for c in candles
            if c.delivery_pct >= min_delivery_pct and c.volume >= min_volume
        ]
        leaders.sort(key=lambda c: c.delivery_pct, reverse=True)
        return leaders

    def get_volume_leaders(
        self,
        target_date: datetime | None = None,
        top_n: int = 50,
    ) -> list[Candle]:
        """
        Get top N stocks by volume from today's bhavcopy.

        Returns:
            List of top N Candle dataclasses sorted by volume descending.
        """
        candles = self.fetch(target_date)
        candles.sort(key=lambda c: c.volume, reverse=True)
        return candles[:top_n]
