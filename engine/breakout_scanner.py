"""
Trend-Investorr: Breakout Scanner

Detects price breaking through key technical levels in real-time.
Uses pre-computed indicators (support/resistance, pivots, 52-week H/L,
Bollinger Bands) from the warm-up cache — zero computation on the hot path.

Breakout Types:
    1. Resistance breakout:  price crosses above swing-high resistance
    2. Support breakdown:    price crosses below swing-low support
    3. 52-week high:         price exceeds 52-week high (rare, powerful)
    4. Pivot breakout:       price crosses R1/R2 or S1/S2 levels
    5. Bollinger expansion:  price breaks above upper band after squeeze

Volatility-aware: only triggers if the breakout move is > 0.5 × ATR
(avoids noise from random wicks touching levels).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, TechnicalIndicators, Tick


class BreakoutScanResult:
    """Result of scanning a single stock for breakouts."""

    __slots__ = (
        "symbol", "token", "breakout_type", "level_name",
        "level_price", "current_price", "distance_pct",
        "strength", "is_breakout",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        breakout_type: str,
        level_name: str,
        level_price: float,
        current_price: float,
        distance_pct: float,
        strength: SignalStrength,
        is_breakout: bool,
    ):
        self.symbol = symbol
        self.token = token
        self.breakout_type = breakout_type  # "resistance" | "support" | "52w" | "pivot" | "bollinger"
        self.level_name = level_name
        self.level_price = level_price
        self.current_price = current_price
        self.distance_pct = distance_pct
        self.strength = strength
        self.is_breakout = is_breakout

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "type": self.breakout_type,
            "level": self.level_name,
            "level_price": round(self.level_price, 2),
            "price": round(self.current_price, 2),
            "distance_pct": round(self.distance_pct, 2),
            "strength": self.strength.value,
        }


class BreakoutScanner:
    """
    Real-time breakout detection engine.

    Compares live tick prices against pre-computed technical levels
    to detect breakouts the moment they happen.

    Usage:
        scanner = BreakoutScanner()
        scanner.set_indicator_cache(indicator_cache)
        result = scanner.on_tick(tick)  # Returns BreakoutScanResult or None
    """

    def __init__(
        self,
        min_atr_multiple: float = 0.5,
        cooldown_minutes: int = 60,
    ):
        """
        Args:
            min_atr_multiple: Minimum ATR multiple for the breakout move
                              to be considered valid (filters noise).
            cooldown_minutes: Cooldown per stock to avoid re-signaling.
        """
        self._min_atr_multiple = min_atr_multiple
        self._cooldown_minutes = cooldown_minutes

        # Pre-computed indicators: token → TechnicalIndicators
        self._indicators: dict[str, TechnicalIndicators] = {}

        # Track previous close to detect crossover (token → last_price)
        self._prev_prices: dict[str, float] = {}

        # Cooldowns: token → last_signal_time
        self._cooldowns: dict[str, datetime] = {}

        # Stats
        self._total_ticks: int = 0
        self._breakouts_detected: int = 0

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        """Load pre-computed indicators from warm-up cache."""
        self._indicators = cache
        logger.info(f"BreakoutScanner: loaded indicators for {len(cache)} stocks")

    def on_tick(self, tick: Tick) -> BreakoutScanResult | None:
        """
        Process a tick and check for breakouts against pre-computed levels.

        Returns BreakoutScanResult if a breakout is detected, None otherwise.
        """
        self._total_ticks += 1

        indicators = self._indicators.get(tick.token)
        if not indicators:
            return None

        # Get previous price for crossover detection
        prev_price = self._prev_prices.get(tick.token, 0.0)
        self._prev_prices[tick.token] = tick.ltp

        if prev_price <= 0:
            return None  # First tick — no crossover possible

        # Check cooldown
        if self._is_on_cooldown(tick.token):
            return None

        # ATR filter — breakout move must be meaningful
        atr = indicators.atr_14
        if atr <= 0:
            atr = tick.ltp * 0.02  # Fallback: 2% of price

        min_move = atr * self._min_atr_multiple

        # ── Check all breakout levels ──

        # 1. Resistance breakout (swing-high)
        if indicators.resistance_level > 0:
            result = self._check_level_cross(
                tick, prev_price, indicators.resistance_level,
                direction="above", level_name="Resistance",
                breakout_type="resistance", min_move=min_move,
            )
            if result:
                return result

        # 2. Support breakdown (swing-low)
        if indicators.support_level > 0:
            result = self._check_level_cross(
                tick, prev_price, indicators.support_level,
                direction="below", level_name="Support",
                breakout_type="support", min_move=min_move,
            )
            if result:
                return result

        # 3. 52-week high breakout (powerful — highest conviction)
        if indicators.high_52w > 0:
            result = self._check_level_cross(
                tick, prev_price, indicators.high_52w,
                direction="above", level_name="52W High",
                breakout_type="52w_high", min_move=min_move,
            )
            if result:
                return result

        # 4. Pivot R1 breakout
        if indicators.pivot_r1 > 0:
            result = self._check_level_cross(
                tick, prev_price, indicators.pivot_r1,
                direction="above", level_name="Pivot R1",
                breakout_type="pivot_r1", min_move=min_move,
            )
            if result:
                return result

        # 5. Pivot R2 breakout (strong momentum)
        if indicators.pivot_r2 > 0:
            result = self._check_level_cross(
                tick, prev_price, indicators.pivot_r2,
                direction="above", level_name="Pivot R2",
                breakout_type="pivot_r2", min_move=min_move,
            )
            if result:
                return result

        # 6. Pivot S1 breakdown
        if indicators.pivot_s1 > 0:
            result = self._check_level_cross(
                tick, prev_price, indicators.pivot_s1,
                direction="below", level_name="Pivot S1",
                breakout_type="pivot_s1", min_move=min_move,
            )
            if result:
                return result

        # 7. Bollinger upper band breakout (volatility expansion)
        if indicators.bollinger_upper > 0:
            # Bollinger squeeze detection: narrow width → breakout
            is_squeezed = indicators.bollinger_width > 0 and indicators.bollinger_width < 0.05
            if is_squeezed:
                result = self._check_level_cross(
                    tick, prev_price, indicators.bollinger_upper,
                    direction="above", level_name="Bollinger Upper (Squeeze)",
                    breakout_type="bollinger_squeeze", min_move=min_move,
                )
                if result:
                    return result

        return None

    def _check_level_cross(
        self,
        tick: Tick,
        prev_price: float,
        level: float,
        direction: str,
        level_name: str,
        breakout_type: str,
        min_move: float,
    ) -> BreakoutScanResult | None:
        """
        Check if price crossed a level between the previous and current tick.

        For 'above': prev_price <= level AND current_price > level
        For 'below': prev_price >= level AND current_price < level
        """
        current = tick.ltp

        if direction == "above":
            crossed = prev_price <= level and current > level
            move = current - level
        else:
            crossed = prev_price >= level and current < level
            move = level - current

        if not crossed:
            return None

        # Filter: move must be at least min_move (ATR-based)
        if abs(move) < min_move:
            return None

        distance_pct = ((current - level) / level) * 100
        strength = self._determine_strength(breakout_type, abs(distance_pct))

        self._breakouts_detected += 1
        self._cooldowns[tick.token] = datetime.now(IST)

        logger.info(
            f"🔓 BREAKOUT: {tick.symbol} {direction} {level_name} "
            f"₹{level:.2f} → ₹{current:.2f} ({distance_pct:+.2f}%) | "
            f"{breakout_type} | {strength.value}"
        )

        return BreakoutScanResult(
            symbol=tick.symbol,
            token=tick.token,
            breakout_type=breakout_type,
            level_name=level_name,
            level_price=level,
            current_price=current,
            distance_pct=distance_pct,
            strength=strength,
            is_breakout=True,
        )

    @staticmethod
    def _determine_strength(breakout_type: str, distance_pct: float) -> SignalStrength:
        """Determine signal strength from breakout type and distance."""
        # 52-week high breakouts are inherently stronger
        if breakout_type == "52w_high":
            return SignalStrength.EXTREME if distance_pct > 1.0 else SignalStrength.STRONG

        if breakout_type.startswith("pivot_r2") or breakout_type.startswith("pivot_s2"):
            return SignalStrength.STRONG

        if distance_pct > 2.0:
            return SignalStrength.STRONG
        elif distance_pct > 1.0:
            return SignalStrength.MODERATE
        else:
            return SignalStrength.WEAK

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def to_observation(self, result: BreakoutScanResult) -> "Observation":
        """Convert to an Observation for the StrategyEngine (not a trade decision)."""
        from data.models.market_data import Observation

        direction = (
            "bullish"
            if result.breakout_type in ("resistance", "52w_high", "pivot_r1", "pivot_r2", "bollinger_squeeze")
            else "bearish"
        )

        confidence_map = {
            SignalStrength.EXTREME: 0.90,
            SignalStrength.STRONG: 0.75,
            SignalStrength.MODERATE: 0.60,
            SignalStrength.WEAK: 0.45,
        }

        return Observation(
            symbol=result.symbol,
            token=result.token,
            source=SignalSource.BREAKOUT,
            direction=direction,
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=result.breakout_type,
            details={
                "level_name": result.level_name,
                "level_price": result.level_price,
                "distance_pct": round(result.distance_pct, 2),
            },
        )

    def to_signal(self, result: BreakoutScanResult, volume_ratio: float = 1.0) -> Signal:
        """Convert a BreakoutScanResult to a Signal for the pipeline."""
        signal_type = (
            SignalType.BUY
            if result.breakout_type in ("resistance", "52w_high", "pivot_r1", "pivot_r2", "bollinger_squeeze")
            else SignalType.SELL
        )

        # Score based on breakout strength + distance
        base_score = 0.5
        if result.strength == SignalStrength.EXTREME:
            base_score = 0.8
        elif result.strength == SignalStrength.STRONG:
            base_score = 0.65
        elif result.strength == SignalStrength.MODERATE:
            base_score = 0.55

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=min(base_score, 1.0),
            price=result.current_price,
            source=SignalSource.BREAKOUT,
            volume_ratio=volume_ratio,
            metadata={
                "breakout_type": result.breakout_type,
                "level_name": result.level_name,
                "level_price": result.level_price,
                "distance_pct": round(result.distance_pct, 2),
            },
        )

    def reset_daily(self) -> None:
        """Reset for a new trading day."""
        self._prev_prices.clear()
        self._cooldowns.clear()
        self._breakouts_detected = 0
        logger.info("BreakoutScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "indicators_loaded": len(self._indicators),
            "ticks_processed": self._total_ticks,
            "breakouts_detected": self._breakouts_detected,
        }
