"""
Trend-Investorr: Momentum Scanner

Detects momentum-based trading opportunities that DON'T require
a volume spike — filling the blind spot left by VolumeScanner.

Signals:
    1. RSI Divergence:    Price makes new low but RSI doesn't → bullish reversal
    2. MACD Crossover:    MACD histogram flips from negative to positive (or vice versa)
    3. ADX Trend Start:   ADX rises above 25 with +DI/-DI crossover
    4. Supertrend Flip:   Direction changes from bearish(-1) to bullish(+1)
    5. EMA Cross:         EMA20 crosses above EMA50 (golden cross, short-term)

Uses pre-computed indicators from warm-up + live tick prices.
The key insight: these signals often precede volume spikes by hours.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, TechnicalIndicators, Tick


class MomentumScanResult:
    """Result of momentum signal detection."""

    __slots__ = (
        "symbol", "token", "signal_name", "direction",
        "current_price", "indicator_values", "strength",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        signal_name: str,
        direction: str,
        current_price: float,
        indicator_values: dict[str, float],
        strength: SignalStrength,
    ):
        self.symbol = symbol
        self.token = token
        self.signal_name = signal_name    # "rsi_divergence", "macd_crossover", etc.
        self.direction = direction        # "bullish" or "bearish"
        self.current_price = current_price
        self.indicator_values = indicator_values
        self.strength = strength


class MomentumScanner:
    """
    Momentum-based signal detection engine.

    Compares live tick prices against pre-computed indicators to detect
    momentum shifts that precede major moves.

    Usage:
        scanner = MomentumScanner()
        scanner.set_indicator_cache(indicator_cache)
        result = scanner.on_tick(tick)
    """

    def __init__(self, cooldown_minutes: int = 60):
        self._cooldown_minutes = cooldown_minutes
        self._indicators: dict[str, TechnicalIndicators] = {}
        self._prev_prices: dict[str, float] = {}
        self._cooldowns: dict[str, datetime] = {}
        self._signals_detected: int = 0

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        self._indicators = cache
        logger.info(f"MomentumScanner: loaded indicators for {len(cache)} stocks")

    def on_tick(self, tick: Tick) -> MomentumScanResult | None:
        """
        Process a tick and check for momentum signals.

        Checks multiple momentum patterns in priority order.
        Returns the first match (strongest signals checked first).
        """
        indicators = self._indicators.get(tick.token)
        if not indicators:
            return None

        if self._is_on_cooldown(tick.token):
            return None

        prev_price = self._prev_prices.get(tick.token, 0.0)
        self._prev_prices[tick.token] = tick.ltp

        if prev_price <= 0:
            return None

        # Check patterns in priority order

        # 1. RSI divergence (oversold + price recovering)
        result = self._check_rsi_extremes(tick, indicators)
        if result:
            return result

        # 2. Supertrend flip (direction change is very reliable)
        result = self._check_supertrend(tick, indicators)
        if result:
            return result

        # 3. MACD crossover
        result = self._check_macd(tick, indicators)
        if result:
            return result

        # 4. ADX trend start
        result = self._check_adx_trend(tick, indicators)
        if result:
            return result

        # 5. EMA alignment with price confirmation
        result = self._check_ema_alignment(tick, indicators, prev_price)
        if result:
            return result

        return None

    def _check_rsi_extremes(
        self, tick: Tick, ind: TechnicalIndicators,
    ) -> MomentumScanResult | None:
        """
        RSI extreme + price divergence detection.

        Bullish: RSI < 30 (oversold) AND price is bouncing up
        Bearish: RSI > 70 (overbought) AND price is rolling over
        """
        if ind.rsi_14 < 30:
            # Oversold — check if price is above support (bouncing)
            if ind.support_level > 0 and tick.ltp > ind.support_level:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="rsi_oversold_bounce",
                    direction="bullish",
                    current_price=tick.ltp,
                    indicator_values={
                        "rsi": ind.rsi_14,
                        "support": ind.support_level,
                    },
                    strength=SignalStrength.STRONG,
                )

        if ind.rsi_14 > 75:
            # Heavily overbought — check if price is below resistance (reversal)
            if ind.resistance_level > 0 and tick.ltp < ind.resistance_level:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="rsi_overbought_reversal",
                    direction="bearish",
                    current_price=tick.ltp,
                    indicator_values={
                        "rsi": ind.rsi_14,
                        "resistance": ind.resistance_level,
                    },
                    strength=SignalStrength.STRONG,
                )

        return None

    def _check_supertrend(
        self, tick: Tick, ind: TechnicalIndicators,
    ) -> MomentumScanResult | None:
        """
        Supertrend direction confirmation with live price.

        Bullish Supertrend + price above supertrend value → strong uptrend.
        Only triggers when there's clear confirmation (price well above/below).
        """
        if ind.supertrend <= 0:
            return None

        price = tick.ltp
        distance_pct = abs(price - ind.supertrend) / ind.supertrend * 100

        # Need at least 0.5% distance from supertrend for confirmation
        if distance_pct < 0.5:
            return None

        if ind.supertrend_direction == 1 and price > ind.supertrend:
            # Bullish supertrend with confirmed uptrend
            # Additional filter: MACD should also be supportive
            if ind.macd_histogram > 0:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="supertrend_bullish_confirmed",
                    direction="bullish",
                    current_price=price,
                    indicator_values={
                        "supertrend": ind.supertrend,
                        "direction": ind.supertrend_direction,
                        "macd_hist": ind.macd_histogram,
                        "distance_pct": round(distance_pct, 2),
                    },
                    strength=SignalStrength.MODERATE,
                )

        if ind.supertrend_direction == -1 and price < ind.supertrend:
            # Bearish supertrend with confirmed downtrend
            if ind.macd_histogram < 0:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="supertrend_bearish_confirmed",
                    direction="bearish",
                    current_price=price,
                    indicator_values={
                        "supertrend": ind.supertrend,
                        "direction": ind.supertrend_direction,
                        "macd_hist": ind.macd_histogram,
                        "distance_pct": round(distance_pct, 2),
                    },
                    strength=SignalStrength.MODERATE,
                )

        return None

    def _check_macd(
        self, tick: Tick, ind: TechnicalIndicators,
    ) -> MomentumScanResult | None:
        """
        MACD crossover detection.

        Bullish: MACD histogram positive AND ADX > 20 (trending)
        Bearish: MACD histogram negative AND ADX > 20
        """
        if ind.macd_histogram == 0 or ind.adx_14 < 20:
            return None

        # MACD histogram strongly positive (above 0 and growing)
        if ind.macd_histogram > 0 and ind.macd_line > ind.macd_signal:
            # Additional filter: price should be above EMA20
            if ind.ema_20 > 0 and tick.ltp > ind.ema_20:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="macd_bullish_crossover",
                    direction="bullish",
                    current_price=tick.ltp,
                    indicator_values={
                        "macd_line": ind.macd_line,
                        "macd_signal": ind.macd_signal,
                        "macd_histogram": ind.macd_histogram,
                        "adx": ind.adx_14,
                    },
                    strength=SignalStrength.MODERATE,
                )

        if ind.macd_histogram < 0 and ind.macd_line < ind.macd_signal:
            if ind.ema_20 > 0 and tick.ltp < ind.ema_20:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="macd_bearish_crossover",
                    direction="bearish",
                    current_price=tick.ltp,
                    indicator_values={
                        "macd_line": ind.macd_line,
                        "macd_signal": ind.macd_signal,
                        "macd_histogram": ind.macd_histogram,
                        "adx": ind.adx_14,
                    },
                    strength=SignalStrength.MODERATE,
                )

        return None

    def _check_adx_trend(
        self, tick: Tick, ind: TechnicalIndicators,
    ) -> MomentumScanResult | None:
        """
        ADX trend strength detection.

        ADX > 30 = very strong trend. Combined with Supertrend direction
        for confirmation.
        """
        if ind.adx_14 < 30:
            return None

        if ind.supertrend_direction == 1 and ind.macd_histogram > 0:
            self._emit(tick.token)
            return MomentumScanResult(
                symbol=tick.symbol,
                token=tick.token,
                signal_name="strong_trend_bullish",
                direction="bullish",
                current_price=tick.ltp,
                indicator_values={
                    "adx": ind.adx_14,
                    "supertrend_dir": ind.supertrend_direction,
                },
                strength=SignalStrength.STRONG,
            )

        if ind.supertrend_direction == -1 and ind.macd_histogram < 0:
            self._emit(tick.token)
            return MomentumScanResult(
                symbol=tick.symbol,
                token=tick.token,
                signal_name="strong_trend_bearish",
                direction="bearish",
                current_price=tick.ltp,
                indicator_values={
                    "adx": ind.adx_14,
                    "supertrend_dir": ind.supertrend_direction,
                },
                strength=SignalStrength.STRONG,
            )

        return None

    def _check_ema_alignment(
        self, tick: Tick, ind: TechnicalIndicators, prev_price: float,
    ) -> MomentumScanResult | None:
        """
        EMA alignment: price crossing above EMA20 when EMA20 > EMA50.

        This is a pullback buy signal — stock in uptrend (EMA20 > EMA50),
        price pulled back below EMA20, now reclaiming it.
        """
        if ind.ema_20 <= 0 or ind.ema_50 <= 0:
            return None

        # Pullback buy: EMA20 > EMA50 AND price crosses above EMA20
        if ind.ema_20 > ind.ema_50:
            if prev_price <= ind.ema_20 and tick.ltp > ind.ema_20:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="ema_pullback_buy",
                    direction="bullish",
                    current_price=tick.ltp,
                    indicator_values={
                        "ema_20": ind.ema_20,
                        "ema_50": ind.ema_50,
                    },
                    strength=SignalStrength.WEAK,
                )

        # Pullback sell: EMA20 < EMA50 AND price crosses below EMA20
        if ind.ema_20 < ind.ema_50:
            if prev_price >= ind.ema_20 and tick.ltp < ind.ema_20:
                self._emit(tick.token)
                return MomentumScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="ema_pullback_sell",
                    direction="bearish",
                    current_price=tick.ltp,
                    indicator_values={
                        "ema_20": ind.ema_20,
                        "ema_50": ind.ema_50,
                    },
                    strength=SignalStrength.WEAK,
                )

        return None

    def _emit(self, token: str) -> None:
        self._signals_detected += 1
        self._cooldowns[token] = datetime.now(IST)

    def to_observation(self, result: MomentumScanResult) -> "Observation":
        """Convert to Observation for StrategyEngine."""
        from data.models.market_data import Observation
        confidence_map = {SignalStrength.STRONG: 0.75, SignalStrength.MODERATE: 0.60, SignalStrength.WEAK: 0.45}
        return Observation(
            symbol=result.symbol, token=result.token,
            source=SignalSource.MOMENTUM,
            direction=result.direction,
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=result.signal_name,
            details=result.indicator_values,
        )

    def to_signal(self, result: MomentumScanResult) -> Signal:
        """Convert a MomentumScanResult to a Signal for the pipeline."""
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        score_map = {
            SignalStrength.STRONG: 0.65,
            SignalStrength.MODERATE: 0.55,
            SignalStrength.WEAK: 0.45,
        }
        score = score_map.get(result.strength, 0.5)

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=score,
            price=result.current_price,
            source=SignalSource.MOMENTUM,
            metadata={
                "momentum_signal": result.signal_name,
                "direction": result.direction,
                **result.indicator_values,
            },
        )

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        self._prev_prices.clear()
        self._cooldowns.clear()
        self._signals_detected = 0
        logger.info("MomentumScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "indicators_loaded": len(self._indicators),
            "signals_detected": self._signals_detected,
        }
