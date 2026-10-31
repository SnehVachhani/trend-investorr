"""
Trend-Investorr: Instrument Master Loader

Downloads and caches the Angel One instrument master list.
This file contains all tradable instruments with their tokens,
symbols, lot sizes, and exchange info.

The instrument list is refreshed daily during pre-market warmup
and cached locally as a CSV to avoid repeated downloads.

Raises:
    DataFetchError: If download fails after retries.
    InstrumentNotFoundError: If a symbol/token lookup fails.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from loguru import logger

from core.constants import Exchange, IST
from core.exceptions import DataFetchError, InstrumentNotFoundError
from core.universe import is_equity_stock
from data.models.market_data import Stock


class InstrumentLoader:
    """
    Downloads, caches, and provides lookup for the SmartAPI instrument master list.

    The master list maps symbol names to SmartAPI tokens needed for
    WebSocket subscriptions and order placement.

    Usage:
        loader = InstrumentLoader(cache_dir=Path("data_cache"))
        loader.load()  # Downloads or loads from cache

        stock = loader.get_by_symbol("RELIANCE")
        stock = loader.get_by_token("2885")
        all_nse = loader.get_all_by_exchange(Exchange.NSE)
    """

    # Angel One instrument list URL
    INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

    def __init__(self, cache_dir: Path):
        """
        Args:
            cache_dir: Directory to cache the instrument list CSV.
        """
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # In-memory lookups
        self._by_symbol: dict[str, Stock] = {}       # "RELIANCE" -> Stock
        self._by_token: dict[str, Stock] = {}         # "2885" -> Stock
        self._by_exchange_token: dict[tuple[str, Exchange], Stock] = {}
        self._by_exchange: dict[Exchange, list[Stock]] = {}

        self._df: pd.DataFrame | None = None          # Raw DataFrame
        self._loaded = False
        self._last_loaded_at: datetime | None = None
        self._total_instruments: int = 0

    @property
    def cache_file(self) -> Path:
        """Today's cached instrument file."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return self._cache_dir / f"instruments_{today}.json"

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def total_instruments(self) -> int:
        return self._total_instruments

    def load(self, force_refresh: bool = False) -> int:
        """
        Load the instrument master list.

        Uses cached file if available and from today, otherwise downloads fresh.

        Args:
            force_refresh: Force download even if cache exists.

        Returns:
            Number of instruments loaded.

        Raises:
            DataFetchError: If download fails and no cache is available.
        """
        if not force_refresh and self.cache_file.exists():
            try:
                return self._load_from_cache()
            except Exception as e:
                logger.warning(f"Cache load failed, will download fresh: {e}")

        return self._download_and_cache()

    def _download_and_cache(self) -> int:
        """Download fresh instrument list from Angel One.

        Raises:
            DataFetchError: If download fails after all retries.
        """
        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Downloading instrument master list (attempt {attempt}/{max_retries})")

                response = requests.get(
                    self.INSTRUMENT_URL,
                    timeout=30,
                    headers={"User-Agent": "TrendInvestorr/1.0"},
                )
                response.raise_for_status()

                data = response.json()

                if not data or not isinstance(data, list):
                    raise DataFetchError(
                        f"Invalid instrument data: expected list, got {type(data).__name__}",
                        details={"response_length": len(data) if data else 0},
                    )

                # Save to cache
                try:
                    self.cache_file.write_text(
                        json.dumps(data),
                        encoding="utf-8",
                    )
                    logger.debug(f"Instrument cache saved: {self.cache_file}")
                except OSError as e:
                    logger.warning(f"Failed to cache instruments (non-critical): {e}")

                # Build lookups
                count = self._build_lookups(data)

                self._loaded = True
                self._last_loaded_at = datetime.now(IST)
                logger.info(
                    f"Instrument master loaded: {count} instruments | "
                    f"NSE: {len(self._by_exchange.get(Exchange.NSE, []))} | "
                    f"BSE: {len(self._by_exchange.get(Exchange.BSE, []))}"
                )
                return count

            except DataFetchError:
                raise  # Our own error, don't retry

            except requests.exceptions.Timeout:
                last_error = DataFetchError(
                    f"Instrument download timed out (attempt {attempt})",
                    details={"url": self.INSTRUMENT_URL, "timeout": 30},
                )
                logger.warning(f"Download timeout (attempt {attempt}/{max_retries})")

            except requests.exceptions.RequestException as e:
                last_error = DataFetchError(
                    f"Instrument download failed: {e}",
                    details={"url": self.INSTRUMENT_URL, "error": str(e)},
                )
                logger.warning(f"Download error (attempt {attempt}/{max_retries}): {e}")

            except Exception as e:
                last_error = DataFetchError(
                    f"Unexpected error loading instruments: {e}",
                    details={"error": type(e).__name__, "message": str(e)},
                )
                logger.warning(f"Unexpected error (attempt {attempt}/{max_retries}): {e}")

            # Wait before retry (exponential backoff)
            if attempt < max_retries:
                wait_time = 2 ** attempt
                logger.debug(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)

        # All retries exhausted — try loading stale cache as fallback
        stale_caches = sorted(self._cache_dir.glob("instruments_*.json"), reverse=True)
        for stale_file in stale_caches:
            try:
                logger.warning(f"Falling back to stale cache: {stale_file.name}")
                return self._load_from_file(stale_file)
            except Exception:
                continue

        raise last_error or DataFetchError("Instrument download failed after all retries")

    def _load_from_cache(self) -> int:
        """Load instruments from today's cache file.

        Raises:
            DataFetchError: If cache file is corrupt or unreadable.
        """
        return self._load_from_file(self.cache_file)

    def _load_from_file(self, file_path: Path) -> int:
        """Load instruments from a JSON file.

        Raises:
            DataFetchError: If file is corrupt or unreadable.
        """
        try:
            raw = file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            count = self._build_lookups(data)
            self._loaded = True
            self._last_loaded_at = datetime.now(IST)
            logger.info(f"Instruments loaded from cache: {file_path.name} ({count} instruments)")
            return count
        except json.JSONDecodeError as e:
            raise DataFetchError(
                f"Instrument cache corrupted: {e}",
                details={"file": str(file_path)},
            )
        except OSError as e:
            raise DataFetchError(
                f"Cannot read instrument cache: {e}",
                details={"file": str(file_path)},
            )

    def _build_lookups(self, data: list[dict]) -> int:
        """Build in-memory lookup dictionaries from raw instrument data."""
        self._by_symbol.clear()
        self._by_token.clear()
        self._by_exchange_token.clear()
        self._by_exchange.clear()

        count = 0
        for item in data:
            try:
                symbol = str(item.get("symbol") or "").strip()
                token = str(item.get("token") or "").strip()
                name = str(item.get("name") or symbol).strip()
                exch_str = str(item.get("exch_seg") or "").upper().strip()
                instrument_type = str(item.get("instrumenttype") or "").upper().strip()
                # BSE cash-market symbols do not carry NSE's -EQ suffix. Store
                # their regular equity series as EQ, regardless of punctuation
                # that may be part of the BSE trading symbol itself.
                series = (
                    "IDX"
                    if instrument_type == "AMXIDX"
                    else "EQ"
                    if exch_str == "BSE"
                    else symbol.split("-")[-1] if "-" in symbol else "EQ"
                )
                lot_size = int(item.get("lotsize", 1) or 1)

                if not symbol or not token:
                    continue

                exchange = self._parse_exchange(exch_str)
                if exchange is None:
                    continue

                stock = Stock(
                    symbol=symbol,
                    token=token,
                    name=name,
                    exchange=exchange,
                    series=series,
                    lot_size=lot_size,
                    instrument_type=instrument_type,
                )

                # Primary lookup: symbol@exchange (handles duplicates across exchanges)
                key = f"{symbol}@{exchange.value}"
                self._by_symbol[key] = stock
                # Keep an unqualified lookup NSE-first so cross-listed names
                # do not become dependent on provider row order.
                if exchange == Exchange.NSE or symbol not in self._by_symbol:
                    self._by_symbol[symbol] = stock
                self._by_exchange_token[(token, exchange)] = stock
                # SmartAPI tokens are not globally unique across exchanges.
                # Preserve a stable NSE-first fallback for legacy token-only
                # callers, while supporting explicit exchange lookups.
                if exchange == Exchange.NSE or token not in self._by_token:
                    self._by_token[token] = stock

                # Exchange grouping
                if exchange not in self._by_exchange:
                    self._by_exchange[exchange] = []
                self._by_exchange[exchange].append(stock)

                count += 1

            except (KeyError, ValueError, TypeError) as e:
                # Skip malformed entries, don't crash
                continue

        self._total_instruments = count
        return count

    @staticmethod
    def _parse_exchange(exch_str: str) -> Exchange | None:
        """Parse exchange string to Exchange enum."""
        match exch_str:
            case "NSE":
                return Exchange.NSE
            case "BSE":
                return Exchange.BSE
            case "NFO":
                return Exchange.NFO
            case "MCX":
                return Exchange.MCX
            case _:
                return None

    # ── Lookups ──────────────────────────────────────────

    def get_by_symbol(self, symbol: str, exchange: Exchange = Exchange.NSE) -> Stock:
        """
        Look up a stock by its trading symbol.

        Args:
            symbol: Stock symbol (e.g., "RELIANCE").
            exchange: Exchange to look up on (default: NSE).

        Returns:
            Stock dataclass.

        Raises:
            InstrumentNotFoundError: If the symbol is not found.
        """
        self._ensure_loaded()

        # Try exchange-specific key first
        key = f"{symbol}@{exchange.value}"
        stock = self._by_symbol.get(key)
        if stock:
            return stock

        # Fall back to a plain symbol only when it belongs to the requested
        # exchange; NSE and BSE frequently share the same ticker text.
        stock = self._by_symbol.get(symbol)
        if stock and stock.exchange == exchange:
            return stock

        raise InstrumentNotFoundError(
            symbol,
            exchange=exchange.value,
            loaded_count=self._total_instruments,
        )

    def get_by_token(self, token: str, exchange: Exchange | None = None) -> Stock:
        """
        Look up a stock by its SmartAPI token.

        Args:
            token: SmartAPI instrument token (e.g., "2885").
            exchange: Optional exchange for tokens that exist on both venues.

        Returns:
            Stock dataclass.

        Raises:
            InstrumentNotFoundError: If the token is not found.
        """
        self._ensure_loaded()

        stock = (
            self._by_exchange_token.get((token, exchange))
            if exchange is not None
            else self._by_token.get(token)
        )
        if stock:
            return stock

        raise InstrumentNotFoundError(
            token,
            lookup_type="token",
            loaded_count=self._total_instruments,
        )

    def get_all_by_exchange(self, exchange: Exchange = Exchange.NSE) -> list[Stock]:
        """Get all instruments for a specific exchange."""
        self._ensure_loaded()
        return self._by_exchange.get(exchange, [])

    def search(self, query: str, exchange: Exchange | None = None, max_results: int = 20) -> list[Stock]:
        """
        Search instruments by partial symbol or name match.

        Args:
            query: Partial symbol or name to search for.
            exchange: Optional exchange filter.
            max_results: Maximum results to return.

        Returns:
            List of matching Stock objects.
        """
        self._ensure_loaded()
        query_upper = query.upper()
        results = []

        for stock in self._by_token.values():
            if exchange and stock.exchange != exchange:
                continue

            if query_upper in stock.symbol.upper() or query_upper in stock.name.upper():
                results.append(stock)
                if len(results) >= max_results:
                    break

        return results

    # All tradeable equity series on NSE:
    #   EQ = Regular equity (normal delivery + intraday)
    #   BE = Trade-to-trade / Book Entry (compulsory delivery, no intraday)
    #   SM = SME platform stocks (NSE Emerge)
    #   ST = SME trade-to-trade
    #   BZ = Trade-to-trade under ASM/GSM surveillance
    EQUITY_SERIES = frozenset({"EQ", "BE", "SM", "ST", "BZ"})

    def get_nse_equity_symbols(self) -> list[str]:
        """Get all NSE equity symbols (all tradeable equity series)."""
        self._ensure_loaded()
        return [
            s.symbol for s in self._by_exchange.get(Exchange.NSE, [])
            if not s.instrument_type and s.series in self.EQUITY_SERIES
        ]

    def get_bse_equity_symbols(self) -> list[str]:
        """Get BSE cash-market company equities, excluding index instruments."""
        self._ensure_loaded()
        return [
            stock.symbol
            for stock in self._by_exchange.get(Exchange.BSE, [])
            if not stock.instrument_type
            and is_equity_stock({
                "exchange": stock.exchange.value,
                "series": stock.series,
                "symbol": stock.symbol,
                "name": stock.name,
                "token": stock.token,
            })
        ]

    def sync_to_db(self, db) -> int:
        """
        Sync the loaded NSE and BSE company equities to PostgreSQL.
        
        Args:
            db: Database instance to use.
            
        Returns:
            Number of stocks synced.
        """
        self._ensure_loaded()
        from storage.models import StockStore
        
        # NSE series identify company equities; BSE's cash-market scrips have
        # no series suffix, so the instrument type and shared equity rules are
        # used there. Keep the Nifty and Sensex index rows too: the enhanced
        # momentum scanner uses them as market-context benchmarks, while their
        # IDX series keeps them out of every company-equity universe.
        equity_stocks = []
        benchmark_stocks = []
        for exchange in (Exchange.NSE, Exchange.BSE):
            for stock in self._by_exchange.get(exchange, []):
                if stock.instrument_type:
                    symbol_key = stock.symbol.strip().upper()
                    name_key = stock.name.strip().upper()
                    if stock.instrument_type == "AMXIDX" and (
                        (exchange == Exchange.NSE and (
                            symbol_key in {"NIFTY", "NIFTY 50", "NIFTY50"}
                            or name_key in {"NIFTY", "NIFTY 50", "NIFTY50"}
                        ))
                        or (exchange == Exchange.BSE and (
                            symbol_key in {"SENSEX", "BSESN"}
                            or name_key in {"SENSEX", "BSESN"}
                        ))
                    ):
                        benchmark_stocks.append(stock)
                    continue
                if exchange == Exchange.NSE and stock.series not in self.EQUITY_SERIES:
                    continue
                if not is_equity_stock({
                    "exchange": stock.exchange.value,
                    "series": stock.series,
                    "symbol": stock.symbol,
                    "name": stock.name,
                    "token": stock.token,
                }):
                    continue
                equity_stocks.append(stock)

        sync_stocks = equity_stocks + benchmark_stocks
        if not sync_stocks:
            return 0
            
        # Format for batch upsert: (symbol, token, name, exchange, series, industry, lot_size)
        rows = [
            (
                s.symbol, 
                s.token, 
                s.name, 
                s.exchange.value, 
                s.series, 
                s.sector or "", 
                s.lot_size
            ) 
            for s in sync_stocks
        ]
        
        store = StockStore(db)
        store.upsert_batch(rows)
        # The BSE instrument master also contains debt securities whose
        # instrumenttype is blank. Keep those outside the active stock
        # universe even if an earlier broad import inserted them as EQ.
        deactivated_non_equities = db.execute(
            """
            UPDATE stocks
            SET is_active = FALSE, updated_at = NOW()
            WHERE exchange = 'BSE'
              AND series = 'EQ'
              AND LEFT(COALESCE(token, ''), 1) <> '5'
              AND is_active = TRUE
            """
        )
        by_exchange = {
            exchange.value: sum(1 for stock in sync_stocks if stock.exchange == exchange)
            for exchange in (Exchange.NSE, Exchange.BSE)
        }
        logger.info(
            "Synced {} stock/benchmark instruments to the database "
            "(NSE: {}, BSE: {}, deactivated BSE non-equities: {})",
            len(rows), by_exchange["NSE"], by_exchange["BSE"],
            deactivated_non_equities,
        )
        return len(rows)

    def _ensure_loaded(self) -> None:
        """Check that instruments are loaded.

        Raises:
            DataFetchError: If instruments haven't been loaded yet.
        """
        if not self._loaded:
            raise DataFetchError(
                "Instrument list not loaded. Call load() first.",
                details={"loaded": False},
            )

    def __len__(self) -> int:
        return self._total_instruments

    def __repr__(self) -> str:
        return (
            f"InstrumentLoader(loaded={self._loaded}, "
            f"instruments={self._total_instruments}, "
            f"last_loaded={self._last_loaded_at})"
        )
