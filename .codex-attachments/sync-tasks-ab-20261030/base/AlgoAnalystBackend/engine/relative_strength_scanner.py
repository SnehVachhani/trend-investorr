"""
Trend-Investorr: Relative Strength Scanner

Identifies stocks outperforming or underperforming their sector and
the broader market. Relative outperformers tend to continue outperforming.

Uses SectorClassifier's sector heat data and per-stock price changes
to calculate relative strength metrics.

Signals:
    1. Sector Outperformer:  Stock's % change > sector average + threshold
    2. Market Leader:        Stock making new 20-day high while sector is flat
    3. Laggard Catch-Up:     Stock underperforming sector by > N% → mean reversion

Data source: _indicator_cache + live tick prices + SectorClassifier.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, TechnicalIndicators, Tick


class RScanResult:
    """Result of relative strength analysis."""

    __slots__ = (
        "symbol", "token", "pattern", "direction",
        "current_price", "stock_change_pct", "sector_change_pct",
        "relative_strength", "strength",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        pattern: str,
        direction: str,
        current_price: float,
        stock_change_pct: float,
        sector_change_pct: float,
        relative_strength: float,
        strength: SignalStrength,
    ):
        self.symbol = symbol
        self.token = token
        self.pattern = pattern
        self.direction = direction
        self.current_price = current_price
        self.stock_change_pct = stock_change_pct
        self.sector_change_pct = sector_change_pct
        self.relative_strength = relative_strength
        self.strength = strength


class RelativeStrengthScanner:
    """
    Relative strength detection engine.

    Compares individual stock performance against sector and market
    benchmarks to identify leaders and laggards.

    Usage:
        scanner = RelativeStrengthScanner()
        scanner.set_indicator_cache(indicator_cache)
        result = scanner.evaluate(tick, sector_heat)
    """

    def __init__(
        self,
        outperform_threshold_pct: float = 2.0,
        laggard_threshold_pct: float = -3.0,
        cooldown_minutes: int = 120,
    ):
        """
        Args:
            outperform_threshold_pct: Min % outperformance vs sector for signal.
            laggard_threshold_pct: Min % underperformance for laggard signal.
            cooldown_minutes: Cooldown per stock.
        """
        self._outperform_threshold = outperform_threshold_pct
        self._laggard_threshold = laggard_threshold_pct
        self._cooldown_minutes = cooldown_minutes

        self._indicators: dict[str, TechnicalIndicators] = {}
        self._cooldowns: dict[str, datetime] = {}
        self._signals_detected: int = 0

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        self._indicators = cache

    def evaluate(
        self,
        tick: Tick,
        sector_avg_change_pct: float = 0.0,
        market_avg_change_pct: float = 0.0,
    ) -> RScanResult | None:
        """
        Evaluate relative strength for a stock.

        Args:
            tick: Current tick data.
            sector_avg_change_pct: Average % change for the stock's sector today.
            market_avg_change_pct: Average % change for the market today.
        """
        if self._is_on_cooldown(tick.token):
            return None

        indicators = self._indicators.get(tick.token)
        if not indicators:
            return None

        # Calculate stock's change % from previous close
        if tick.close <= 0:
            return None
        stock_change_pct = ((tick.ltp - tick.close) / tick.close) * 100

        # Reference point: use sector avg if available, else market avg
        benchmark = sector_avg_change_pct if sector_avg_change_pct != 0 else market_avg_change_pct
        relative_strength = stock_change_pct - benchmark

        # 1. Sector Outperformer
        if relative_strength >= self._outperform_threshold:
            # Additional: check if stock is near 52-week high (leadership)
            is_near_52w = (
                indicators.high_52w > 0 and
                tick.ltp > indicators.high_52w * 0.95
            )
            strength = SignalStrength.STRONG if is_near_52w else SignalStrength.MODERATE

            self._emit(tick.token)
            return RScanResult(
                symbol=tick.symbol,
                token=tick.token,
                pattern="sector_outperformer",
                direction="bullish",
                current_price=tick.ltp,
                stock_change_pct=stock_change_pct,
                sector_change_pct=benchmark,
                relative_strength=relative_strength,
                strength=strength,
            )

        # 2. Market leader (rising against a falling market)
        if stock_change_pct > 1.0 and benchmark < -0.5:
            self._emit(tick.token)
            return RScanResult(
                symbol=tick.symbol,
                token=tick.token,
                pattern="market_leader",
                direction="bullish",
                current_price=tick.ltp,
                stock_change_pct=stock_change_pct,
                sector_change_pct=benchmark,
                relative_strength=relative_strength,
                strength=SignalStrength.STRONG,
            )

        # 3. Laggard catch-up (underperforming stock in strong sector)
        if relative_strength <= self._laggard_threshold and benchmark > 1.0:
            # Mean reversion: sector is up but this stock is down
            if indicators.rsi_14 < 40:  # Oversold laggard
                self._emit(tick.token)
                return RScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    pattern="laggard_catchup",
                    direction="bullish",
                    current_price=tick.ltp,
                    stock_change_pct=stock_change_pct,
                    sector_change_pct=benchmark,
                    relative_strength=relative_strength,
                    strength=SignalStrength.WEAK,
                )

        return None

    def to_signal(self, result: RScanResult) -> Signal:
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        score_map = {
            SignalStrength.STRONG: 0.65,
            SignalStrength.MODERATE: 0.55,
            SignalStrength.WEAK: 0.45,
        }

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=score_map.get(result.strength, 0.5),
            price=result.current_price,
            source=SignalSource.RELATIVE_STRENGTH,
            metadata={
                "rs_pattern": result.pattern,
                "stock_change_pct": round(result.stock_change_pct, 2),
                "sector_change_pct": round(result.sector_change_pct, 2),
                "relative_strength": round(result.relative_strength, 2),
            },
        )

    def _emit(self, token: str) -> None:
        self._signals_detected += 1
        self._cooldowns[token] = datetime.now(IST)

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        self._cooldowns.clear()
        self._signals_detected = 0
        logger.info("RelativeStrengthScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {"signals_detected": self._signals_detected}

    def to_observation(self, result: RScanResult) -> "Observation":
        """Convert to Observation for StrategyEngine."""
        from data.models.market_data import Observation
        confidence_map = {SignalStrength.STRONG: 0.70, SignalStrength.MODERATE: 0.55, SignalStrength.WEAK: 0.40}
        return Observation(
            symbol=result.symbol, token=result.token,
            source=SignalSource.RELATIVE_STRENGTH,
            direction=result.direction,
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=result.pattern,
            details={
                "stock_change_pct": round(result.stock_change_pct, 2),
                "sector_change_pct": round(result.sector_change_pct, 2),
                "relative_strength": round(result.relative_strength, 2),
            },
        )
