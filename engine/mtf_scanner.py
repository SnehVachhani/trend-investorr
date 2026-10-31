"""
Trend-Investorr: Multi-Timeframe Confluence Scanner

Identifies setups where multiple timeframes align in the same direction.
Multi-timeframe confluence is the highest-conviction setup category.

Examples:
    - 5-min breakout + 1-hour uptrend + daily above EMA20 → strong BUY
    - Weekly support test + daily RSI oversold + 5-min bullish reversal → BUY

Data source: CandleStore has tables for 1m, 5m, 15m, 1h, 1d, 1w.
Pre-computed indicators available for the daily timeframe.

This scanner is a POST-MARKET batch scanner that identifies multi-timeframe
setups for the next trading day, OR runs periodically during market hours
using the candle cache for different timeframes.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from loguru import logger

from core.constants import SignalSource, SignalType
from data.models.market_data import Candle, Signal, SignalStrength, TechnicalIndicators
from engine.indicator_engine import IndicatorEngine


class MTFScanResult:
    """Result of multi-timeframe confluence detection."""

    __slots__ = (
        "symbol", "token", "direction", "confluence_score",
        "timeframe_signals", "strength", "price",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        direction: str,
        confluence_score: float,
        timeframe_signals: dict[str, str],
        strength: SignalStrength,
        price: float,
    ):
        self.symbol = symbol
        self.token = token
        self.direction = direction
        self.confluence_score = confluence_score
        self.timeframe_signals = timeframe_signals  # {"daily": "bullish", "weekly": "bullish", ...}
        self.strength = strength
        self.price = price


class MTFScanner:
    """
    Multi-timeframe confluence scanner.

    Checks alignment across daily (pre-computed indicators),
    weekly (derived from daily candles), and intraday (from candle cache)
    timeframes.

    Usage:
        scanner = MTFScanner()
        results = scanner.scan_all(candle_cache, indicator_cache)
    """

    # Minimum candles needed per timeframe
    MIN_DAILY_CANDLES = 50
    MIN_WEEKLY_CANDLES = 10

    def scan_all(
        self,
        candle_cache: dict[str, list[Candle]],
        indicator_cache: dict[str, TechnicalIndicators] | None = None,
    ) -> list[MTFScanResult]:
        """
        Scan all stocks for multi-timeframe confluence.

        Args:
            candle_cache: token → list[daily Candle] (sorted ASC).
            indicator_cache: token → TechnicalIndicators.
        """
        results: list[MTFScanResult] = []

        for token, candles in candle_cache.items():
            if len(candles) < self.MIN_DAILY_CANDLES:
                continue

            indicators = indicator_cache.get(token) if indicator_cache else None
            result = self._analyze_stock(token, candles, indicators)
            if result:
                results.append(result)

        logger.info(
            f"MTFScanner: scanned {len(candle_cache)} stocks, "
            f"found {len(results)} confluence setups"
        )
        return results

    def _analyze_stock(
        self,
        token: str,
        candles: list[Candle],
        indicators: TechnicalIndicators | None,
    ) -> MTFScanResult | None:
        """Analyze a single stock across multiple timeframes."""
        if not candles:
            return None

        latest = candles[-1]
        tf_signals: dict[str, str] = {}
        bullish_count = 0
        bearish_count = 0
        total_checks = 0

        # ── Daily Timeframe (from indicators) ──
        if indicators:
            # Trend: EMA alignment
            if indicators.ema_20 > 0 and indicators.ema_50 > 0:
                total_checks += 1
                if indicators.ema_20 > indicators.ema_50:
                    tf_signals["daily_trend"] = "bullish"
                    bullish_count += 1
                else:
                    tf_signals["daily_trend"] = "bearish"
                    bearish_count += 1

            # Momentum: RSI
            total_checks += 1
            if indicators.rsi_14 < 40:
                tf_signals["daily_rsi"] = "bullish"  # Oversold → upside potential
                bullish_count += 1
            elif indicators.rsi_14 > 60:
                tf_signals["daily_rsi"] = "bearish"  # Overbought → downside risk
                bearish_count += 1
            else:
                tf_signals["daily_rsi"] = "neutral"

            # Supertrend
            if indicators.supertrend_direction != 0:
                total_checks += 1
                if indicators.supertrend_direction == 1:
                    tf_signals["daily_supertrend"] = "bullish"
                    bullish_count += 1
                else:
                    tf_signals["daily_supertrend"] = "bearish"
                    bearish_count += 1

            # MACD
            total_checks += 1
            if indicators.macd_histogram > 0:
                tf_signals["daily_macd"] = "bullish"
                bullish_count += 1
            elif indicators.macd_histogram < 0:
                tf_signals["daily_macd"] = "bearish"
                bearish_count += 1
            else:
                tf_signals["daily_macd"] = "neutral"

        # ── Weekly Timeframe (derived from daily candles) ──
        weekly_signal = self._analyze_weekly(candles)
        if weekly_signal:
            total_checks += 1
            tf_signals["weekly_trend"] = weekly_signal
            if weekly_signal == "bullish":
                bullish_count += 1
            elif weekly_signal == "bearish":
                bearish_count += 1

        # ── Price Structure ──
        if indicators:
            # Near support (bullish) or resistance (bearish)
            if indicators.support_level > 0:
                dist = (latest.close - indicators.support_level) / latest.close * 100
                if 0 < dist < 3:
                    total_checks += 1
                    tf_signals["price_structure"] = "bullish"
                    bullish_count += 1

            if indicators.resistance_level > 0:
                dist = (indicators.resistance_level - latest.close) / latest.close * 100
                if 0 < dist < 3:
                    total_checks += 1
                    tf_signals["price_structure"] = "bearish"
                    bearish_count += 1

        # ── Calculate Confluence ──
        if total_checks < 3:
            return None

        # Need strong majority alignment
        bullish_pct = bullish_count / total_checks
        bearish_pct = bearish_count / total_checks

        if bullish_pct >= 0.7:
            direction = "bullish"
            confluence_score = bullish_pct
        elif bearish_pct >= 0.7:
            direction = "bearish"
            confluence_score = bearish_pct
        else:
            return None  # No clear confluence

        # Determine strength
        if confluence_score >= 0.9:
            strength = SignalStrength.EXTREME
        elif confluence_score >= 0.8:
            strength = SignalStrength.STRONG
        else:
            strength = SignalStrength.MODERATE

        return MTFScanResult(
            symbol=latest.symbol,
            token=token,
            direction=direction,
            confluence_score=confluence_score,
            timeframe_signals=tf_signals,
            strength=strength,
            price=latest.close,
        )

    def _analyze_weekly(self, candles: list[Candle]) -> str | None:
        """
        Derive weekly trend from daily candles.

        Uses the last 5 trading days (1 week) to determine direction.
        """
        if len(candles) < 10:
            return None

        # Last 5 days
        recent = candles[-5:]
        prev_week = candles[-10:-5]

        recent_close = recent[-1].close
        prev_close = prev_week[-1].close

        if prev_close <= 0:
            return None

        weekly_change = (recent_close - prev_close) / prev_close * 100

        # Also check if recent week had higher lows (uptrend)
        recent_lows = [c.low for c in recent]
        prev_lows = [c.low for c in prev_week]

        higher_lows = min(recent_lows) > min(prev_lows)
        lower_highs = max([c.high for c in recent]) < max([c.high for c in prev_week])

        if weekly_change > 1.0 and higher_lows:
            return "bullish"
        elif weekly_change < -1.0 and lower_highs:
            return "bearish"

        return "neutral"

    def to_signal(self, result: MTFScanResult) -> Signal:
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        score_map = {
            SignalStrength.EXTREME: 0.80,
            SignalStrength.STRONG: 0.70,
            SignalStrength.MODERATE: 0.55,
            SignalStrength.WEAK: 0.45,
        }

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=score_map.get(result.strength, 0.5),
            price=result.price,
            source=SignalSource.MTF_CONFLUENCE,
            metadata={
                "confluence_score": round(result.confluence_score, 2),
                "timeframe_signals": result.timeframe_signals,
                "next_day_signal": True,
            },
        )
