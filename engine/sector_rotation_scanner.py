"""
Trend-Investorr: Sector Rotation Scanner

Detects sector-level money flow patterns that indicate rotation
between sectors. Uses SectorClassifier's heat map data.

Signals:
    1. Hot Sector Entry:      Sector heat > 0.7 + advancing ratio > 2:1
    2. Sector Divergence:     One stock declining while rest of sector advances
    3. Rotation Detection:    Money flowing out of sector A into sector B
    4. Sector Breakout:       Multiple stocks in same sector showing signals

Data source: SectorClassifier.compute_sector_heat() — already exists.
This scanner adds temporal tracking to detect CHANGES in sector heat.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength


class SectorRotationResult:
    """Result of sector rotation analysis."""

    __slots__ = (
        "sector_name", "pattern", "direction",
        "current_heat", "previous_heat", "heat_delta",
        "top_stocks", "strength",
    )

    def __init__(
        self,
        sector_name: str,
        pattern: str,
        direction: str,
        current_heat: float,
        previous_heat: float,
        heat_delta: float,
        top_stocks: list[str],
        strength: SignalStrength,
    ):
        self.sector_name = sector_name
        self.pattern = pattern
        self.direction = direction
        self.current_heat = current_heat
        self.previous_heat = previous_heat
        self.heat_delta = heat_delta
        self.top_stocks = top_stocks
        self.strength = strength


class SectorRotationScanner:
    """
    Sector-level rotation detection engine.

    Tracks sector heat scores over time to detect money flowing
    between sectors. Produces sector-level signals that can be
    used to boost individual stock signals in hot sectors.

    Usage:
        scanner = SectorRotationScanner()
        results = scanner.evaluate(sector_heat_map)
    """

    def __init__(
        self,
        hot_threshold: float = 0.7,
        cold_threshold: float = 0.3,
        rotation_min_delta: float = 0.3,
        scan_interval_minutes: int = 15,
    ):
        """
        Args:
            hot_threshold: Sector heat score above which sector is "hot".
            cold_threshold: Sector heat score below which sector is "cold".
            rotation_min_delta: Min heat delta between scans for rotation.
            scan_interval_minutes: Minimum minutes between scans.
        """
        self._hot_threshold = hot_threshold
        self._cold_threshold = cold_threshold
        self._rotation_min_delta = rotation_min_delta
        self._scan_interval = scan_interval_minutes

        # Historical heat: sector_name → previous heat score
        self._prev_heat: dict[str, float] = {}

        # Last scan time
        self._last_scan: datetime | None = None

        self._signals_detected: int = 0

    def evaluate(
        self,
        sector_heat_map: dict[str, Any],
    ) -> list[SectorRotationResult]:
        """
        Evaluate all sectors for rotation patterns.

        Args:
            sector_heat_map: Output from SectorClassifier.compute_sector_heat()
                             Dict of sector_name → {score, advancing, declining, ...}

        Returns:
            List of SectorRotationResult for sectors showing rotation.
        """
        now = datetime.now(IST)

        # Throttle: only scan every N minutes
        if self._last_scan:
            elapsed = (now - self._last_scan).total_seconds() / 60
            if elapsed < self._scan_interval:
                return []
        self._last_scan = now

        results: list[SectorRotationResult] = []

        for sector_name, heat_data in sector_heat_map.items():
            if not isinstance(heat_data, dict):
                continue

            current_heat = heat_data.get("score", 0.0)
            advancing = heat_data.get("advancing", 0)
            declining = heat_data.get("declining", 0)
            top_stocks = heat_data.get("top_movers", [])

            prev_heat = self._prev_heat.get(sector_name, 0.5)
            heat_delta = current_heat - prev_heat
            self._prev_heat[sector_name] = current_heat

            # If top_stocks are objects, extract symbols
            if top_stocks and isinstance(top_stocks[0], dict):
                stock_symbols = [s.get("symbol", "") for s in top_stocks[:5]]
            else:
                stock_symbols = top_stocks[:5] if top_stocks else []

            # 1. Hot Sector Entry
            if current_heat >= self._hot_threshold and prev_heat < self._hot_threshold:
                total = advancing + declining
                adv_ratio = advancing / max(declining, 1)

                if adv_ratio >= 2.0:
                    results.append(SectorRotationResult(
                        sector_name=sector_name,
                        pattern="hot_sector_entry",
                        direction="bullish",
                        current_heat=current_heat,
                        previous_heat=prev_heat,
                        heat_delta=heat_delta,
                        top_stocks=stock_symbols,
                        strength=SignalStrength.STRONG,
                    ))
                    self._signals_detected += 1

            # 2. Sector cooling off (was hot, turning cold)
            if current_heat <= self._cold_threshold and prev_heat > self._cold_threshold:
                results.append(SectorRotationResult(
                    sector_name=sector_name,
                    pattern="sector_cooling",
                    direction="bearish",
                    current_heat=current_heat,
                    previous_heat=prev_heat,
                    heat_delta=heat_delta,
                    top_stocks=stock_symbols,
                    strength=SignalStrength.MODERATE,
                ))
                self._signals_detected += 1

            # 3. Rapid heat change (rotation)
            if abs(heat_delta) >= self._rotation_min_delta:
                direction = "bullish" if heat_delta > 0 else "bearish"
                results.append(SectorRotationResult(
                    sector_name=sector_name,
                    pattern="rotation_detected",
                    direction=direction,
                    current_heat=current_heat,
                    previous_heat=prev_heat,
                    heat_delta=heat_delta,
                    top_stocks=stock_symbols,
                    strength=SignalStrength.MODERATE,
                ))
                self._signals_detected += 1

        if results:
            logger.info(
                f"🔄 SECTOR ROTATION: {len(results)} sector signals detected | "
                f"sectors: {[r.sector_name for r in results]}"
            )

        return results

    def get_sector_boost(self, sector_name: str) -> float:
        """
        Get a confidence boost for stocks in a hot sector.

        Returns a multiplier (1.0 = no boost, 1.2 = 20% boost, etc.)
        """
        heat = self._prev_heat.get(sector_name, 0.5)
        if heat >= self._hot_threshold:
            return 1.2  # 20% boost for hot sectors
        elif heat <= self._cold_threshold:
            return 0.8  # 20% penalty for cold sectors
        return 1.0

    def to_signals(
        self,
        result: SectorRotationResult,
        sector_stocks: list[dict],
    ) -> list[Signal]:
        """
        Convert sector result to per-stock signals for stocks in that sector.

        Args:
            result: The sector rotation result.
            sector_stocks: List of stock dicts in the sector
                           [{symbol, token, ...}].
        """
        signals = []
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        score_map = {
            SignalStrength.STRONG: 0.60,
            SignalStrength.MODERATE: 0.50,
            SignalStrength.WEAK: 0.40,
        }
        base_score = score_map.get(result.strength, 0.5)

        for stock in sector_stocks[:10]:  # Limit to top 10 in sector
            signals.append(Signal(
                symbol=stock.get("symbol", ""),
                token=stock.get("token", ""),
                signal_type=signal_type,
                strength=result.strength,
                score=base_score,
                price=0.0,  # Will be filled from live data
                source=SignalSource.SECTOR_ROTATION,
                metadata={
                    "sector": result.sector_name,
                    "rotation_pattern": result.pattern,
                    "sector_heat": round(result.current_heat, 2),
                    "heat_delta": round(result.heat_delta, 2),
                },
            ))

        return signals

    def reset_daily(self) -> None:
        # Keep prev_heat across days for multi-day trend tracking
        self._last_scan = None
        self._signals_detected = 0
        logger.info("SectorRotationScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "sectors_tracked": len(self._prev_heat),
            "signals_detected": self._signals_detected,
        }

    def to_observations(self, result: SectorRotationResult) -> list:
        """Convert sector-level result to per-stock Observations for StrategyEngine."""
        from data.models.market_data import Observation
        observations = []
        confidence_map = {SignalStrength.STRONG: 0.65, SignalStrength.MODERATE: 0.50, SignalStrength.WEAK: 0.35}
        conf = confidence_map.get(result.strength, 0.5)

        for stock_symbol in result.top_stocks:
            observations.append(Observation(
                symbol=stock_symbol, token="",
                source=SignalSource.SECTOR_ROTATION,
                direction=result.direction,
                confidence=conf,
                price=0.0,
                pattern_name=result.pattern,
                details={
                    "sector": result.sector_name,
                    "sector_heat": round(result.current_heat, 2),
                    "heat_delta": round(result.heat_delta, 2),
                },
            ))
        return observations
