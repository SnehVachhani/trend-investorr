"""
Trend-Investorr: Stock Filters

Pre-scan filters to narrow the universe of stocks before volume scanning.
Filters remove illiquid, penny, or out-of-range stocks early to save
processing time and avoid false signals.

Filters are applied in order (pipeline pattern):
    All stocks → Price Filter → Market Cap Filter → Liquidity Filter
    → Series Filter → Sector Filter → Filtered Universe

All thresholds are read from AppConfigManager (runtime-tunable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from core.constants import Exchange
from data.models.market_data import Candle, Stock


@dataclass
class FilterConfig:
    """Runtime filter thresholds — populated from app_config table."""

    min_price: float = 1.0             # Skip sub-rupee penny stocks
    max_price: float = float("inf")    # No upper price limit
    min_market_cap_cr: float = 0.0     # No minimum market cap
    max_market_cap_cr: float = float("inf") # No maximum market cap
    min_avg_volume: int = 10000        # Min 30d avg daily volume (shares)
    min_delivery_pct: float = 0.0      # Min delivery % (0 = no filter)
    allowed_series: set[str] | None = None  # None = allow all EQ series
    excluded_sectors: set[str] | None = None  # None = no exclusions
    allowed_exchanges: set[Exchange] | None = None  # None = NSE only default

    def __post_init__(self):
        if self.allowed_series is None:
            self.allowed_series = {"EQ"}
        if self.allowed_exchanges is None:
            self.allowed_exchanges = {Exchange.NSE}


@dataclass(frozen=True)
class FilterResult:
    """Result of filtering a single stock."""

    passed: bool
    symbol: str
    rejected_by: str = ""       # Which filter rejected it
    reason: str = ""            # Human-readable reason


class StockFilter:
    """
    Pipeline-based stock filter.

    Applies multiple filter stages sequentially. A stock must pass
    ALL filters to be included in the scan universe.

    Usage:
        f = StockFilter(config=FilterConfig(min_price=100))
        result = f.apply(stock, latest_candle, avg_volume)
        filtered = f.filter_batch(stocks_with_data)
    """

    def __init__(self, config: FilterConfig | None = None):
        self._config = config or FilterConfig()
        self._stats = {
            "total_checked": 0,
            "passed": 0,
            "rejected_price": 0,
            "rejected_market_cap": 0,
            "rejected_volume": 0,
            "rejected_series": 0,
            "rejected_exchange": 0,
            "rejected_sector": 0,
        }

    def apply(
        self,
        stock: Stock,
        latest_candle: Candle | None = None,
        avg_volume_30d: int = 0,
    ) -> FilterResult:
        """
        Apply all filters to a single stock.

        Args:
            stock: The stock to check.
            latest_candle: Most recent daily candle (for price checks).
            avg_volume_30d: 30-day average daily volume.

        Returns:
            FilterResult indicating pass/fail with reason.
        """
        self._stats["total_checked"] += 1

        # 1. Exchange filter
        if self._config.allowed_exchanges and stock.exchange not in self._config.allowed_exchanges:
            self._stats["rejected_exchange"] += 1
            return FilterResult(
                passed=False, symbol=stock.symbol,
                rejected_by="exchange",
                reason=f"Exchange {stock.exchange.value} not in allowed list",
            )

        # 2. Series filter
        if self._config.allowed_series and stock.series not in self._config.allowed_series:
            self._stats["rejected_series"] += 1
            return FilterResult(
                passed=False, symbol=stock.symbol,
                rejected_by="series",
                reason=f"Series '{stock.series}' not in allowed list",
            )

        # 3. Price filter (uses latest candle close price)
        if latest_candle:
            price = latest_candle.close
            if price < self._config.min_price:
                self._stats["rejected_price"] += 1
                return FilterResult(
                    passed=False, symbol=stock.symbol,
                    rejected_by="price_low",
                    reason=f"Price ₹{price:.2f} below min ₹{self._config.min_price}",
                )
            if price > self._config.max_price:
                self._stats["rejected_price"] += 1
                return FilterResult(
                    passed=False, symbol=stock.symbol,
                    rejected_by="price_high",
                    reason=f"Price ₹{price:.2f} above max ₹{self._config.max_price}",
                )

        # 4. Market cap filter
        if stock.market_cap_cr > 0:
            if stock.market_cap_cr < self._config.min_market_cap_cr:
                self._stats["rejected_market_cap"] += 1
                return FilterResult(
                    passed=False, symbol=stock.symbol,
                    rejected_by="market_cap_low",
                    reason=f"Market cap ₹{stock.market_cap_cr:.0f}Cr below min ₹{self._config.min_market_cap_cr:.0f}Cr",
                )
            if stock.market_cap_cr > self._config.max_market_cap_cr:
                self._stats["rejected_market_cap"] += 1
                return FilterResult(
                    passed=False, symbol=stock.symbol,
                    rejected_by="market_cap_high",
                    reason=f"Market cap ₹{stock.market_cap_cr:.0f}Cr above max ₹{self._config.max_market_cap_cr:.0f}Cr",
                )

        # 5. Volume / liquidity filter
        if avg_volume_30d > 0 and avg_volume_30d < self._config.min_avg_volume:
            self._stats["rejected_volume"] += 1
            return FilterResult(
                passed=False, symbol=stock.symbol,
                rejected_by="volume",
                reason=f"Avg volume {avg_volume_30d:,} below min {self._config.min_avg_volume:,}",
            )

        # 6. Sector exclusion filter
        if self._config.excluded_sectors and stock.sector in self._config.excluded_sectors:
            self._stats["rejected_sector"] += 1
            return FilterResult(
                passed=False, symbol=stock.symbol,
                rejected_by="sector",
                reason=f"Sector '{stock.sector}' is excluded",
            )

        # Passed all filters
        self._stats["passed"] += 1
        return FilterResult(passed=True, symbol=stock.symbol)

    def filter_batch(
        self,
        stocks: list[tuple[Stock, Candle | None, int]],
    ) -> list[Stock]:
        """
        Filter a batch of stocks.

        Args:
            stocks: List of (Stock, latest_candle, avg_volume_30d) tuples.

        Returns:
            List of Stock objects that passed all filters.
        """
        passed = []
        for stock, candle, avg_vol in stocks:
            result = self.apply(stock, candle, avg_vol)
            if result.passed:
                passed.append(stock)

        logger.info(
            f"Filter results: {len(passed)}/{len(stocks)} passed | "
            f"price={self._stats['rejected_price']}, "
            f"mcap={self._stats['rejected_market_cap']}, "
            f"vol={self._stats['rejected_volume']}, "
            f"series={self._stats['rejected_series']}, "
            f"sector={self._stats['rejected_sector']} rejected"
        )
        return passed

    def update_config(self, config: FilterConfig) -> None:
        """Update filter config (e.g., from app_config table refresh)."""
        self._config = config
        logger.debug(f"Filter config updated: price={config.min_price}-{config.max_price}")

    @property
    def stats(self) -> dict[str, int]:
        """Get filter statistics."""
        return dict(self._stats)

    def reset_stats(self) -> None:
        """Reset filter statistics (call at start of each scan cycle)."""
        for key in self._stats:
            self._stats[key] = 0
