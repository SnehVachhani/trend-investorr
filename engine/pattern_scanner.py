"""
Trend-Investorr: Candle Pattern Scanner

Recognizes classic candlestick patterns on daily and intraday candles
for entry/exit timing.

Patterns Detected:
    1. Bullish Engulfing:    Small bearish → large bullish candle
    2. Bearish Engulfing:    Small bullish → large bearish candle
    3. Hammer:               Long lower wick at support (reversal)
    4. Shooting Star:        Long upper wick at resistance (reversal)
    5. Inside Bar:           Today's range within yesterday's (squeeze)
    6. Doji at Key Level:    Indecision candle near S/R

Data source: _candle_cache (daily candles) — already loaded at warm-up.
This is a BATCH scanner that runs post-market for next-day signals.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from loguru import logger

from core.constants import SignalSource, SignalType
from data.models.market_data import Candle, Signal, SignalStrength, TechnicalIndicators


class PatternScanResult:
    """Result of candle pattern detection."""

    __slots__ = (
        "symbol", "token", "pattern_name", "direction",
        "price", "confidence", "strength", "details",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        pattern_name: str,
        direction: str,
        price: float,
        confidence: float,
        strength: SignalStrength,
        details: dict[str, Any],
    ):
        self.symbol = symbol
        self.token = token
        self.pattern_name = pattern_name
        self.direction = direction
        self.price = price
        self.confidence = confidence
        self.strength = strength
        self.details = details


class PatternScanner:
    """
    Candlestick pattern recognition engine.

    Scans daily candle data for classic reversal and continuation patterns.
    Combines pattern detection with S/R level context from indicators.

    Usage:
        scanner = PatternScanner()
        results = scanner.scan_all(candle_cache, indicator_cache)
    """

    def __init__(
        self,
        min_body_ratio: float = 0.01,
        engulfing_min_body_ratio: float = 1.5,
    ):
        """
        Args:
            min_body_ratio: Minimum body/range ratio to consider non-doji.
            engulfing_min_body_ratio: Engulfing candle body must be this
                                      multiple of previous candle's body.
        """
        self._min_body_ratio = min_body_ratio
        self._engulfing_ratio = engulfing_min_body_ratio

    def scan_all(
        self,
        candle_cache: dict[str, list[Candle]],
        indicator_cache: dict[str, TechnicalIndicators] | None = None,
    ) -> list[PatternScanResult]:
        """
        Scan all stocks for candle patterns.

        Args:
            candle_cache: token → list[Candle] (daily, sorted ASC).
            indicator_cache: token → TechnicalIndicators (optional, for S/R context).
        """
        results: list[PatternScanResult] = []

        for token, candles in candle_cache.items():
            if len(candles) < 5:
                continue

            indicators = indicator_cache.get(token) if indicator_cache else None
            stock_results = self._scan_stock(token, candles, indicators)
            results.extend(stock_results)

        logger.info(
            f"PatternScanner: scanned {len(candle_cache)} stocks, "
            f"found {len(results)} patterns"
        )
        return results

    def _scan_stock(
        self,
        token: str,
        candles: list[Candle],
        indicators: TechnicalIndicators | None,
    ) -> list[PatternScanResult]:
        """Scan a single stock for all patterns."""
        results: list[PatternScanResult] = []

        # Latest 2 candles for pattern detection
        curr = candles[-1]
        prev = candles[-2]

        # Pre-calculate metrics
        curr_body = curr.body_size
        curr_range = curr.range
        prev_body = prev.body_size
        prev_range = prev.range

        if curr_range <= 0 or prev_range <= 0:
            return results

        curr_body_ratio = curr_body / curr_range
        upper_wick = curr.high - max(curr.open, curr.close)
        lower_wick = min(curr.open, curr.close) - curr.low

        # 1. Bullish Engulfing
        if (prev.is_bearish and curr.is_bullish and
                curr_body > prev_body * self._engulfing_ratio and
                curr.close > prev.open and curr.open < prev.close):
            conf = 0.65
            # Boost if near support
            if indicators and indicators.support_level > 0:
                dist = abs(curr.low - indicators.support_level) / curr.low * 100
                if dist < 2:
                    conf += 0.1
            results.append(PatternScanResult(
                symbol=curr.symbol, token=token,
                pattern_name="bullish_engulfing",
                direction="bullish",
                price=curr.close,
                confidence=min(conf, 1.0),
                strength=SignalStrength.STRONG,
                details={"body_ratio": round(curr_body / max(prev_body, 0.01), 2)},
            ))

        # 2. Bearish Engulfing
        if (prev.is_bullish and curr.is_bearish and
                curr_body > prev_body * self._engulfing_ratio and
                curr.open > prev.close and curr.close < prev.open):
            conf = 0.65
            if indicators and indicators.resistance_level > 0:
                dist = abs(curr.high - indicators.resistance_level) / curr.high * 100
                if dist < 2:
                    conf += 0.1
            results.append(PatternScanResult(
                symbol=curr.symbol, token=token,
                pattern_name="bearish_engulfing",
                direction="bearish",
                price=curr.close,
                confidence=min(conf, 1.0),
                strength=SignalStrength.STRONG,
                details={"body_ratio": round(curr_body / max(prev_body, 0.01), 2)},
            ))

        # 3. Hammer (at support level)
        if (lower_wick > curr_body * 2 and upper_wick < curr_body * 0.5
                and curr_body_ratio > self._min_body_ratio):
            conf = 0.55
            if indicators and indicators.support_level > 0:
                dist = abs(curr.low - indicators.support_level) / curr.low * 100
                if dist < 3:
                    conf += 0.15
            results.append(PatternScanResult(
                symbol=curr.symbol, token=token,
                pattern_name="hammer",
                direction="bullish",
                price=curr.close,
                confidence=min(conf, 1.0),
                strength=SignalStrength.MODERATE,
                details={"lower_wick_ratio": round(lower_wick / max(curr_body, 0.01), 2)},
            ))

        # 4. Shooting Star (at resistance level)
        if (upper_wick > curr_body * 2 and lower_wick < curr_body * 0.5
                and curr_body_ratio > self._min_body_ratio):
            conf = 0.55
            if indicators and indicators.resistance_level > 0:
                dist = abs(curr.high - indicators.resistance_level) / curr.high * 100
                if dist < 3:
                    conf += 0.15
            results.append(PatternScanResult(
                symbol=curr.symbol, token=token,
                pattern_name="shooting_star",
                direction="bearish",
                price=curr.close,
                confidence=min(conf, 1.0),
                strength=SignalStrength.MODERATE,
                details={"upper_wick_ratio": round(upper_wick / max(curr_body, 0.01), 2)},
            ))

        # 5. Inside Bar (volatility contraction → breakout imminent)
        if (curr.high <= prev.high and curr.low >= prev.low):
            # Direction determined by trend
            direction = "bullish"
            if indicators and indicators.supertrend_direction == -1:
                direction = "bearish"
            results.append(PatternScanResult(
                symbol=curr.symbol, token=token,
                pattern_name="inside_bar",
                direction=direction,
                price=curr.close,
                confidence=0.50,
                strength=SignalStrength.WEAK,
                details={
                    "prev_range": round(prev_range, 2),
                    "curr_range": round(curr_range, 2),
                    "contraction_pct": round((1 - curr_range / prev_range) * 100, 1),
                },
            ))

        # 6. Doji at key level (small body, indecision)
        if curr_body_ratio < 0.1 and curr_range > 0:
            # Check if at S/R level
            if indicators:
                near_support = (
                    indicators.support_level > 0 and
                    abs(curr.close - indicators.support_level) / curr.close * 100 < 2
                )
                near_resistance = (
                    indicators.resistance_level > 0 and
                    abs(curr.close - indicators.resistance_level) / curr.close * 100 < 2
                )

                if near_support:
                    results.append(PatternScanResult(
                        symbol=curr.symbol, token=token,
                        pattern_name="doji_at_support",
                        direction="bullish",
                        price=curr.close,
                        confidence=0.50,
                        strength=SignalStrength.WEAK,
                        details={"level": "support", "support_price": indicators.support_level},
                    ))
                elif near_resistance:
                    results.append(PatternScanResult(
                        symbol=curr.symbol, token=token,
                        pattern_name="doji_at_resistance",
                        direction="bearish",
                        price=curr.close,
                        confidence=0.50,
                        strength=SignalStrength.WEAK,
                        details={"level": "resistance", "resistance_price": indicators.resistance_level},
                    ))

        return results

    def to_signal(self, result: PatternScanResult) -> Signal:
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=result.confidence,
            price=result.price,
            source=SignalSource.PATTERN,
            metadata={
                "candle_pattern": result.pattern_name,
                "direction": result.direction,
                "next_day_signal": True,
                **result.details,
            },
        )
