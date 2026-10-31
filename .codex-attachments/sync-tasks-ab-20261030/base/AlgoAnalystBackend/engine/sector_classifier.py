"""
Trend-Investorr: Sector Classifier

Maps stocks to sectors using NSE industry classification and
keyword-based matching from config/sectors.json.

The classifier:
    1. Loads sector definitions from sectors.json
    2. Matches stock industry strings to sectors via keyword matching
    3. Provides sector-level aggregation (which sectors are trending)
    4. Tracks sector heat (relative volume/momentum across sectors)

All sector mappings are dynamic — no hardcoded sector lists.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from core.constants import IST
from data.models.market_data import Candle, Stock


class SectorDefinition:
    """A single sector definition loaded from sectors.json."""

    __slots__ = ("name", "display_name", "nifty_index", "keywords")

    def __init__(self, name: str, display_name: str, nifty_index: str, keywords: list[str]):
        self.name = name
        self.display_name = display_name
        self.nifty_index = nifty_index
        self.keywords = [k.upper() for k in keywords]

    def matches(self, industry: str) -> bool:
        """Check if an industry string matches this sector."""
        industry_upper = industry.upper()
        return any(keyword in industry_upper for keyword in self.keywords)


class SectorHeat:
    """Sector-level aggregated metrics for a single snapshot."""

    def __init__(self, sector_name: str):
        self.sector_name = sector_name
        self.stocks_count: int = 0
        self.total_volume: int = 0
        self.avg_volume_ratio: float = 0.0      # Avg volume spike across sector
        self.advancing: int = 0                  # Stocks with positive change
        self.declining: int = 0                  # Stocks with negative change
        self.top_movers: list[str] = []          # Top 5 symbols by volume ratio
        self.avg_change_pct: float = 0.0         # Avg price change %
        self.max_volume_ratio: float = 0.0       # Highest volume spike in sector

    @property
    def advance_decline_ratio(self) -> float:
        """Advancing / Declining ratio. > 1 = bullish sector."""
        if self.declining == 0:
            return float(self.advancing) if self.advancing > 0 else 0.0
        return self.advancing / self.declining

    @property
    def is_bullish(self) -> bool:
        return self.advance_decline_ratio > 1.0

    @property
    def heat_score(self) -> float:
        """
        Composite sector heat score (0.0 - 1.0).

        Higher = more active + more bullish.
        """
        # Weighted combination of volume activity and breadth
        volume_factor = min(self.avg_volume_ratio / 3.0, 1.0)  # Normalize to 3x avg
        breadth_factor = min(self.advance_decline_ratio / 3.0, 1.0)  # Normalize to 3:1
        return (volume_factor * 0.6 + breadth_factor * 0.4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sector": self.sector_name,
            "stocks": self.stocks_count,
            "total_volume": self.total_volume,
            "avg_volume_ratio": round(self.avg_volume_ratio, 2),
            "advancing": self.advancing,
            "declining": self.declining,
            "ad_ratio": round(self.advance_decline_ratio, 2),
            "heat_score": round(self.heat_score, 2),
            "avg_change_pct": round(self.avg_change_pct, 2),
            "top_movers": self.top_movers[:5],
        }


class SectorClassifier:
    """
    Classifies stocks into sectors and tracks sector-level activity.

    Usage:
        classifier = SectorClassifier(sectors_file=Path("config/sectors.json"))
        sector = classifier.classify("Automobile - Cars & Utility Vehicles")
        heat_map = classifier.compute_sector_heat(candles_today, volume_data)
    """

    def __init__(self, sectors_file: Path):
        """
        Args:
            sectors_file: Path to sectors.json config file.
        """
        self._sectors: dict[str, SectorDefinition] = {}
        self._stock_sector_cache: dict[str, str] = {}  # symbol -> sector cache
        self._load_sectors(sectors_file)

    def _load_sectors(self, sectors_file: Path) -> None:
        """Load sector definitions from JSON config.

        Logs warning if file is missing but doesn't crash —
        classification will just return "OTHER" for everything.
        """
        try:
            raw = json.loads(sectors_file.read_text(encoding="utf-8"))
            sector_data = raw.get("sectors", {})

            for name, config in sector_data.items():
                self._sectors[name] = SectorDefinition(
                    name=name,
                    display_name=config.get("display_name", name),
                    nifty_index=config.get("nifty_index", ""),
                    keywords=config.get("keywords", []),
                )

            logger.info(f"Loaded {len(self._sectors)} sector definitions")

        except FileNotFoundError:
            logger.warning(
                f"Sectors config not found: {sectors_file} — "
                "all stocks will be classified as 'OTHER'"
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Invalid sectors config: {e} — using empty sector map")

    def classify(self, industry: str) -> str:
        """
        Classify an industry string into a sector.

        Args:
            industry: NSE industry classification string
                      (e.g., "Automobile - Cars & Utility Vehicles").

        Returns:
            Sector name (e.g., "AUTO"). Returns "OTHER" if no match.
        """
        if not industry:
            return "OTHER"

        for name, sector_def in self._sectors.items():
            if sector_def.matches(industry):
                return name

        return "OTHER"

    def classify_stock(self, stock: Stock) -> str:
        """
        Classify a stock into a sector (with caching).

        Uses stock.industry for classification, caches result by symbol.
        """
        if stock.symbol in self._stock_sector_cache:
            return self._stock_sector_cache[stock.symbol]

        sector = self.classify(stock.industry)
        self._stock_sector_cache[stock.symbol] = sector
        return sector

    def classify_batch(self, stocks: list[Stock]) -> dict[str, str]:
        """
        Classify multiple stocks.

        Returns:
            Dict of symbol -> sector name.
        """
        result = {}
        sector_counts: dict[str, int] = defaultdict(int)

        for stock in stocks:
            sector = self.classify_stock(stock)
            result[stock.symbol] = sector
            sector_counts[sector] += 1

        logger.debug(
            f"Classified {len(stocks)} stocks into {len(sector_counts)} sectors: "
            + ", ".join(f"{s}={c}" for s, c in sorted(sector_counts.items(), key=lambda x: -x[1])[:5])
        )
        return result

    def compute_sector_heat(
        self,
        stock_data: list[tuple[Stock, Candle, float]],
    ) -> dict[str, SectorHeat]:
        """
        Compute sector-level heat metrics from stock data.

        Args:
            stock_data: List of (Stock, today's Candle, volume_ratio) tuples.

        Returns:
            Dict of sector_name -> SectorHeat.
        """
        sector_metrics: dict[str, SectorHeat] = {}
        sector_stocks: dict[str, list[tuple[str, float, float]]] = defaultdict(list)

        for stock, candle, volume_ratio in stock_data:
            sector = self.classify_stock(stock)

            if sector not in sector_metrics:
                sector_metrics[sector] = SectorHeat(sector)

            heat = sector_metrics[sector]
            heat.stocks_count += 1
            heat.total_volume += candle.volume

            change_pct = candle.change_pct
            if change_pct > 0:
                heat.advancing += 1
            elif change_pct < 0:
                heat.declining += 1

            sector_stocks[sector].append((stock.symbol, volume_ratio, change_pct))

        # Compute averages and top movers
        for sector, heat in sector_metrics.items():
            stocks_in_sector = sector_stocks[sector]
            if not stocks_in_sector:
                continue

            volume_ratios = [vr for _, vr, _ in stocks_in_sector]
            change_pcts = [cp for _, _, cp in stocks_in_sector]

            heat.avg_volume_ratio = sum(volume_ratios) / len(volume_ratios)
            heat.max_volume_ratio = max(volume_ratios)
            heat.avg_change_pct = sum(change_pcts) / len(change_pcts)

            # Top movers by volume ratio
            sorted_by_vol = sorted(stocks_in_sector, key=lambda x: -x[1])
            heat.top_movers = [sym for sym, _, _ in sorted_by_vol[:5]]

        return sector_metrics

    def get_trending_sectors(
        self,
        sector_heat: dict[str, SectorHeat],
        min_heat_score: float = 0.3,
        min_stocks: int = 3,
    ) -> list[SectorHeat]:
        """
        Get sectors that are currently trending (high activity).

        Args:
            sector_heat: Output from compute_sector_heat().
            min_heat_score: Minimum heat score to consider trending.
            min_stocks: Minimum stocks in sector to be meaningful.

        Returns:
            List of SectorHeat sorted by heat_score descending.
        """
        trending = [
            heat for heat in sector_heat.values()
            if heat.heat_score >= min_heat_score and heat.stocks_count >= min_stocks
        ]
        trending.sort(key=lambda h: h.heat_score, reverse=True)
        return trending

    def get_sector_display_name(self, sector_name: str) -> str:
        """Get human-readable sector name."""
        sector_def = self._sectors.get(sector_name)
        return sector_def.display_name if sector_def else sector_name

    @property
    def all_sectors(self) -> list[str]:
        """List all defined sector names."""
        return list(self._sectors.keys())

    def __repr__(self) -> str:
        return f"SectorClassifier(sectors={len(self._sectors)}, cached={len(self._stock_sector_cache)})"
