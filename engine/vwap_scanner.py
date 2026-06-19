"""
Trend-Investorr: VWAP Scanner

Detects VWAP-based trading setups using the exchange VWAP already
tracked in VolumeTracker (avg_trade_price from WebSocket).

VWAP is the single most important intraday indicator for institutional
traders. Stocks tend to revert to VWAP, and breakouts from VWAP
are high-probability entries.

Signals:
    1. VWAP Reclaim:     Price drops below VWAP then crosses back above
    2. VWAP Rejection:   Price tests VWAP from below and fails
    3. First VWAP Touch: After gap-up, first pullback to VWAP (buy signal)
    4. VWAP Deviation:   Price > 2 ATR above VWAP → overextended (sell)
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from loguru import logger

from core.constants import IST, MARKET_OPEN, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, TechnicalIndicators, Tick
from engine.volume_scanner import VolumeTracker


class VWAPScanResult:
    """Result of VWAP-based pattern detection."""

    __slots__ = (
        "symbol", "token", "pattern", "direction",
        "current_price", "vwap", "distance_pct",
        "strength",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        pattern: str,
        direction: str,
        current_price: float,
        vwap: float,
        distance_pct: float,
        strength: SignalStrength,
    ):
        self.symbol = symbol
        self.token = token
        self.pattern = pattern
        self.direction = direction
        self.current_price = current_price
        self.vwap = vwap
        self.distance_pct = distance_pct
        self.strength = strength


class VWAPScanner:
    """
    VWAP-based signal detection engine.

    Uses the real exchange VWAP (avg_trade_price from WebSocket) for
    accurate institutional-grade analysis.

    Usage:
        scanner = VWAPScanner()
        scanner.set_indicator_cache(indicator_cache)
        result = scanner.evaluate(tracker, tick)
    """

    # VWAP signals are valid throughout the session
    VWAP_ACTIVE_END = time(15, 15)

    def __init__(
        self,
        deviation_atr_multiple: float = 2.0,
        cooldown_minutes: int = 45,
    ):
        """
        Args:
            deviation_atr_multiple: ATR multiple for overextension detection.
            cooldown_minutes: Cooldown per stock.
        """
        self._deviation_atr = deviation_atr_multiple
        self._cooldown_minutes = cooldown_minutes

        self._indicators: dict[str, TechnicalIndicators] = {}

        # Track price position relative to VWAP (for crossover detection)
        # token → was_above_vwap (True/False)
        self._prev_position: dict[str, bool | None] = {}

        # Track if stock has touched VWAP today (for first-touch detection)
        self._touched_vwap: dict[str, bool] = {}

        self._cooldowns: dict[str, datetime] = {}
        self._signals_detected: int = 0

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        self._indicators = cache

    def evaluate(
        self,
        tracker: VolumeTracker,
        tick: Tick,
    ) -> VWAPScanResult | None:
        """
        Evaluate VWAP patterns for a stock.

        Args:
            tracker: The stock's VolumeTracker (has exchange_vwap).
            tick: The current tick data.
        """
        now = tick.timestamp.time() if isinstance(tick.timestamp, datetime) else tick.timestamp

        if now > self.VWAP_ACTIVE_END or now < MARKET_OPEN:
            return None

        vwap = tracker.exchange_vwap
        if vwap <= 0:
            return None

        if self._is_on_cooldown(tick.token):
            return None

        price = tick.ltp
        distance_pct = ((price - vwap) / vwap) * 100

        # Track position relative to VWAP
        currently_above = price > vwap
        prev = self._prev_position.get(tick.token)
        self._prev_position[tick.token] = currently_above

        if prev is None:
            return None

        # 1. VWAP Reclaim: was below, now above
        if not prev and currently_above:
            # Additional confirmation: buy/sell ratio should support
            if tracker.buy_sell_ratio > 1.1:
                return self._emit_result(
                    tick, vwap, distance_pct,
                    pattern="vwap_reclaim",
                    direction="bullish",
                    strength=SignalStrength.MODERATE,
                )

        # 2. VWAP Breakdown: was above, now below
        if prev and not currently_above:
            if tracker.buy_sell_ratio < 0.9:
                return self._emit_result(
                    tick, vwap, distance_pct,
                    pattern="vwap_breakdown",
                    direction="bearish",
                    strength=SignalStrength.MODERATE,
                )

        # 3. First VWAP touch after gap-up (pullback buy)
        if tracker.gap_pct > 1.0 and not self._touched_vwap.get(tick.token, False):
            # Check if price is approaching VWAP (within 0.2%)
            if abs(distance_pct) < 0.2:
                self._touched_vwap[tick.token] = True
                if price > vwap:
                    return self._emit_result(
                        tick, vwap, distance_pct,
                        pattern="vwap_first_touch_pullback",
                        direction="bullish",
                        strength=SignalStrength.STRONG,
                    )

        # 4. VWAP deviation: overextended above/below
        indicators = self._indicators.get(tick.token)
        if indicators and indicators.atr_14 > 0:
            deviation = abs(price - vwap)
            max_deviation = indicators.atr_14 * self._deviation_atr

            if deviation > max_deviation:
                if price > vwap:
                    # Overextended above VWAP → mean-reversion sell
                    return self._emit_result(
                        tick, vwap, distance_pct,
                        pattern="vwap_overextended_above",
                        direction="bearish",
                        strength=SignalStrength.WEAK,
                    )
                else:
                    # Overextended below VWAP → mean-reversion buy
                    return self._emit_result(
                        tick, vwap, distance_pct,
                        pattern="vwap_overextended_below",
                        direction="bullish",
                        strength=SignalStrength.WEAK,
                    )

        return None

    def _emit_result(
        self,
        tick: Tick,
        vwap: float,
        distance_pct: float,
        pattern: str,
        direction: str,
        strength: SignalStrength,
    ) -> VWAPScanResult:
        self._signals_detected += 1
        self._cooldowns[tick.token] = datetime.now(IST)

        logger.info(
            f"📈 VWAP: {tick.symbol} {pattern} | "
            f"price=₹{tick.ltp:.2f} vs VWAP=₹{vwap:.2f} ({distance_pct:+.2f}%) | "
            f"{direction}"
        )

        return VWAPScanResult(
            symbol=tick.symbol,
            token=tick.token,
            pattern=pattern,
            direction=direction,
            current_price=tick.ltp,
            vwap=vwap,
            distance_pct=distance_pct,
            strength=strength,
        )

    def to_observation(self, result: VWAPScanResult) -> "Observation":
        """Convert to Observation for StrategyEngine."""
        from data.models.market_data import Observation
        confidence_map = {SignalStrength.STRONG: 0.75, SignalStrength.MODERATE: 0.60, SignalStrength.WEAK: 0.45}
        return Observation(
            symbol=result.symbol, token=result.token,
            source=SignalSource.VWAP,
            direction=result.direction,
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=result.pattern,
            details={"vwap": round(result.vwap, 2), "distance_pct": round(result.distance_pct, 2)},
        )

    def to_signal(self, result: VWAPScanResult) -> Signal:
        """Convert to Signal for the pipeline."""
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        score_map = {
            SignalStrength.STRONG: 0.65,
            SignalStrength.MODERATE: 0.55,
            SignalStrength.WEAK: 0.40,
        }
        score = score_map.get(result.strength, 0.5)

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=score,
            price=result.current_price,
            source=SignalSource.VWAP,
            metadata={
                "vwap_pattern": result.pattern,
                "vwap": round(result.vwap, 2),
                "distance_pct": round(result.distance_pct, 2),
            },
        )

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        self._cooldowns.clear()
        self._prev_position.clear()
        self._touched_vwap.clear()
        self._signals_detected = 0
        logger.info("VWAPScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {"signals_detected": self._signals_detected}
