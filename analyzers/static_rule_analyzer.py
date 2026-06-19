"""
Trend-Investorr: Static Rule Analyzer

Pure rule-based signal validation using technical indicators.
No AI, no API keys, no external dependencies, works offline.

Indicators used:
    1. RSI (Relative Strength Index) — momentum oscillator (14-period)
    2. EMA (Exponential Moving Average) — trend direction (20 & 50 period)
    3. VWAP proxy — price vs volume-weighted average
    4. Delivery % — institutional interest proxy
    5. Volume confirmation — ensure volume backs the price move
    6. Price-EMA alignment — trend confirmation

Decision Logic:
    BUY if:  RSI < 70, price > EMA20 > EMA50, volume spike, delivery > 30%
    SELL if: RSI > 70 (overbought) or price < EMA20 < EMA50
    SKIP if: Insufficient data or conflicting signals
"""

from __future__ import annotations

from typing import Any

import numpy as np
from loguru import logger

from analyzers.base import SignalAnalyzer
from core.constants import AnalysisAction, SignalType
from data.models.market_data import AnalysisResult, Candle, Signal, TechnicalIndicators
from engine.indicator_engine import IndicatorEngine


class StaticRuleAnalyzer(SignalAnalyzer):
    """
    Rule-based analyzer using classic technical indicators.

    This is the DEFAULT analyzer — works without any API keys or
    external services. Pure math on historical candle data.

    Scoring:
        Each rule contributes a confidence delta:
        - RSI favorable:       +0.15
        - EMA trend aligned:   +0.20
        - VWAP favorable:      +0.10
        - Delivery strong:     +0.15
        - Volume confirmed:    +0.15
        - Price momentum ok:   +0.10
        - No red flags:        +0.15
        Total possible: 1.0
    """

    # Indicator parameters
    RSI_PERIOD = 14
    EMA_SHORT = 20
    EMA_LONG = 50
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD = 30
    MIN_CANDLES = 50          # Need at least 50 candles for EMA50
    MIN_DELIVERY_PCT = 30.0   # Meaningful delivery percentage

    @property
    def name(self) -> str:
        return "static"

    def analyze(self, signal: Signal, candles: list[Candle]) -> AnalysisResult:
        """
        Analyze using RSI, EMA, VWAP, delivery %, and volume rules.

        Fast path: if pre-computed TechnicalIndicators are available in
        signal.metadata["precomputed_indicators"], uses those directly
        (zero re-computation). Otherwise falls back to computing from candles.

        Returns action=SKIP if insufficient data (< 50 candles).
        Never raises exceptions — all errors caught and returned as SKIP.
        """
        try:
            # ── Fast path: use pre-computed indicators if available ──
            precomputed: TechnicalIndicators | None = signal.metadata.get(
                "precomputed_indicators"
            )

            if precomputed and isinstance(precomputed, TechnicalIndicators):
                current_price = signal.price
                current_rsi = precomputed.rsi_14
                current_ema_short = precomputed.ema_20 if precomputed.ema_20 > 0 else current_price
                current_ema_long = precomputed.ema_50 if precomputed.ema_50 > 0 else current_price
                current_vwap = precomputed.vwap_daily if precomputed.vwap_daily > 0 else current_price
                delivery_pct = precomputed.delivery_pct

                # Build rich indicators dict from pre-computed data
                indicators = precomputed.to_dict()
                indicators["current_price"] = round(current_price, 2)
                indicators["volume_ratio"] = round(signal.volume_ratio, 2)
                # Use ema_short/ema_long aliases for rule evaluation
                indicators["rsi"] = round(current_rsi, 2)
                indicators["ema_short"] = round(current_ema_short, 2)
                indicators["ema_long"] = round(current_ema_long, 2)
                indicators["vwap_proxy"] = round(current_vwap, 2)
                indicators["delivery_pct"] = round(delivery_pct, 2)

            else:
                # ── Slow path: compute from candle data ──
                if not candles or len(candles) < self.MIN_CANDLES:
                    return AnalysisResult(
                        action=AnalysisAction.SKIP,
                        confidence=0.0,
                        reasoning=f"Insufficient data: {len(candles) if candles else 0} candles "
                                  f"(need {self.MIN_CANDLES})",
                        analyzer_name=self.name,
                        signal_symbol=signal.symbol,
                    )

                closes = np.array([c.close for c in candles])
                volumes = np.array([c.volume for c in candles])
                highs = np.array([c.high for c in candles])
                lows = np.array([c.low for c in candles])

                # Compute indicators via IndicatorEngine (single source of truth)
                rsi = IndicatorEngine.compute_rsi(closes)
                ema_short = IndicatorEngine.compute_ema(closes, self.EMA_SHORT)
                ema_long = IndicatorEngine.compute_ema(closes, self.EMA_LONG)
                vwap_proxy = IndicatorEngine.compute_vwap(highs, lows, closes, volumes)

                current_price = closes[-1]
                current_rsi = rsi[-1] if len(rsi) > 0 else 50.0
                current_ema_short = ema_short[-1] if len(ema_short) > 0 else current_price
                current_ema_long = ema_long[-1] if len(ema_long) > 0 else current_price
                current_vwap = vwap_proxy[-1] if len(vwap_proxy) > 0 else current_price
                delivery_pct = signal.metadata.get("delivery_pct", 0.0)
                precomputed = None  # No pre-computed data available

                indicators = {
                    "rsi": round(current_rsi, 2),
                    "ema_short": round(current_ema_short, 2),
                    "ema_long": round(current_ema_long, 2),
                    "vwap_proxy": round(current_vwap, 2),
                    "current_price": round(current_price, 2),
                    "delivery_pct": round(delivery_pct, 2),
                    "volume_ratio": round(signal.volume_ratio, 2),
                }

            # Apply rules based on signal type
            if signal.signal_type == SignalType.BUY:
                return self._evaluate_buy(signal, indicators, current_price,
                                          current_rsi, current_ema_short,
                                          current_ema_long, current_vwap,
                                          delivery_pct, precomputed)
            else:
                return self._evaluate_sell(signal, indicators, current_price,
                                           current_rsi, current_ema_short,
                                           current_ema_long, current_vwap,
                                           precomputed)

        except Exception as e:
            logger.error(f"Static analyzer error for {signal.symbol}: {e}")
            return AnalysisResult(
                action=AnalysisAction.SKIP,
                confidence=0.0,
                reasoning=f"Analysis error: {e}",
                analyzer_name=self.name,
                signal_symbol=signal.symbol,
            )

    def _evaluate_buy(
        self,
        signal: Signal,
        indicators: dict,
        price: float,
        rsi: float,
        ema_short: float,
        ema_long: float,
        vwap: float,
        delivery_pct: float,
        precomputed: TechnicalIndicators | None = None,
    ) -> AnalysisResult:
        """Evaluate a BUY signal against rule-based criteria."""
        confidence = 0.0
        reasons: list[str] = []
        red_flags: list[str] = []

        # 1. RSI check — not overbought
        if rsi < self.RSI_OVERBOUGHT:
            confidence += 0.15
            if rsi < self.RSI_OVERSOLD:
                reasons.append(f"RSI oversold ({rsi:.0f}) — strong reversal potential")
                confidence += 0.05  # Bonus for oversold
            elif rsi < 50:
                reasons.append(f"RSI neutral-low ({rsi:.0f}) — room to run")
            else:
                reasons.append(f"RSI acceptable ({rsi:.0f})")
        else:
            red_flags.append(f"RSI overbought ({rsi:.0f}) — risky entry")

        # 2. EMA trend alignment — price > EMA20 > EMA50 = uptrend
        if price > ema_short > ema_long:
            confidence += 0.20
            reasons.append(f"Strong uptrend: price({price:.0f}) > EMA20({ema_short:.0f}) > EMA50({ema_long:.0f})")
        elif price > ema_short:
            confidence += 0.10
            reasons.append(f"Above short EMA: price > EMA20({ema_short:.0f})")
        elif price > ema_long:
            confidence += 0.05
            reasons.append("Above long EMA but below short — possible consolidation")
        else:
            red_flags.append(f"Below both EMAs — downtrend (price={price:.0f}, EMA20={ema_short:.0f}, EMA50={ema_long:.0f})")

        # 3. VWAP — price above VWAP = buyers in control
        if price > vwap:
            confidence += 0.10
            reasons.append(f"Above VWAP ({vwap:.0f}) — buyers in control")
        else:
            red_flags.append(f"Below VWAP ({vwap:.0f}) — selling pressure")

        # 4. Delivery percentage — institutional interest
        if delivery_pct >= self.MIN_DELIVERY_PCT:
            confidence += 0.15
            reasons.append(f"Delivery {delivery_pct:.0f}% — institutional interest")
        elif delivery_pct > 0:
            confidence += 0.05
            reasons.append(f"Delivery {delivery_pct:.0f}% — moderate")

        # 5. Volume confirmation
        if signal.volume_ratio >= 3.0:
            confidence += 0.15
            reasons.append(f"Strong volume confirmation ({signal.volume_ratio:.1f}x avg)")
        elif signal.volume_ratio >= 2.0:
            confidence += 0.10
            reasons.append(f"Volume confirmation ({signal.volume_ratio:.1f}x avg)")
        else:
            confidence += 0.05

        # 6. No red flags bonus
        if not red_flags:
            confidence += 0.15
            reasons.append("No red flags — clean setup")

        # 7. Enhanced rules from pre-computed indicators (bonus scoring)
        if precomputed:
            # Supertrend confirmation
            if precomputed.supertrend_direction == 1:
                confidence += 0.05
                reasons.append("Supertrend bullish ✓")
            elif precomputed.supertrend_direction == -1:
                red_flags.append("Supertrend bearish")

            # ADX trend strength
            if precomputed.adx_14 > 25:
                confidence += 0.03
                reasons.append(f"Strong trend (ADX={precomputed.adx_14:.0f})")

            # MACD bullish crossover
            if precomputed.macd_histogram > 0:
                confidence += 0.03
                reasons.append("MACD bullish ✓")
            elif precomputed.macd_histogram < 0:
                red_flags.append("MACD bearish")

            # Bollinger position — near lower band = potential bounce
            if precomputed.bollinger_lower > 0 and price < precomputed.bollinger_lower:
                confidence += 0.03
                reasons.append("Below Bollinger lower — potential bounce")

            # Near support level
            if precomputed.support_level > 0:
                distance_to_support = (price - precomputed.support_level) / price * 100
                if 0 < distance_to_support < 2:
                    confidence += 0.05
                    reasons.append(f"Near support ₹{precomputed.support_level:.0f}")

            # Near resistance — warning
            if precomputed.resistance_level > 0:
                distance_to_resistance = (precomputed.resistance_level - price) / price * 100
                if 0 < distance_to_resistance < 1:
                    red_flags.append(f"Near resistance ₹{precomputed.resistance_level:.0f}")

        # Determine action
        confidence = min(confidence, 1.0)

        if red_flags and len(red_flags) >= 2:
            action = AnalysisAction.SKIP
            reasoning = "SKIP: Multiple red flags — " + "; ".join(red_flags)
        elif confidence >= 0.6:
            action = AnalysisAction.BUY
            reasoning = "BUY: " + " | ".join(reasons)
        elif confidence >= 0.4:
            action = AnalysisAction.HOLD
            reasoning = "HOLD: Mixed signals — " + " | ".join(reasons + red_flags)
        else:
            action = AnalysisAction.SKIP
            reasoning = "SKIP: Weak setup — " + " | ".join(reasons + red_flags)

        return AnalysisResult(
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            analyzer_name=self.name,
            signal_symbol=signal.symbol,
            indicators=indicators,
        )

    def _evaluate_sell(
        self,
        signal: Signal,
        indicators: dict,
        price: float,
        rsi: float,
        ema_short: float,
        ema_long: float,
        vwap: float,
        precomputed: TechnicalIndicators | None = None,
    ) -> AnalysisResult:
        """Evaluate a SELL signal against rule-based criteria."""
        confidence = 0.0
        reasons: list[str] = []

        # For sell signals: overbought + downtrend + below VWAP = strong sell
        if rsi > self.RSI_OVERBOUGHT:
            confidence += 0.25
            reasons.append(f"RSI overbought ({rsi:.0f}) — likely reversal")

        if price < ema_short < ema_long:
            confidence += 0.25
            reasons.append("Confirmed downtrend: price < EMA20 < EMA50")
        elif price < ema_short:
            confidence += 0.15
            reasons.append("Below short-term trend (EMA20)")

        if price < vwap:
            confidence += 0.15
            reasons.append("Below VWAP — sellers in control")

        if signal.volume_ratio >= 3.0:
            confidence += 0.15
            reasons.append(f"Heavy distribution volume ({signal.volume_ratio:.1f}x)")

        confidence = min(confidence, 1.0)

        if confidence >= 0.5:
            action = AnalysisAction.SELL
            reasoning = "SELL: " + " | ".join(reasons)
        elif confidence >= 0.3:
            action = AnalysisAction.HOLD
            reasoning = "HOLD: Weak sell signals — " + " | ".join(reasons)
        else:
            action = AnalysisAction.SKIP
            reasoning = "SKIP: No clear sell signal"

        return AnalysisResult(
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            analyzer_name=self.name,
            signal_symbol=signal.symbol,
            indicators=indicators,
        )

    # ── Technical Indicator Calculations ─────────────────
    # All indicator math is now centralized in IndicatorEngine.
    # These thin wrappers are kept for backward compatibility.

    @staticmethod
    def _compute_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
        """Delegate to IndicatorEngine."""
        return IndicatorEngine.compute_rsi(closes, period)

    @staticmethod
    def _compute_ema(data: np.ndarray, period: int) -> np.ndarray:
        """Delegate to IndicatorEngine."""
        return IndicatorEngine.compute_ema(data, period)

    @staticmethod
    def _compute_vwap_proxy(
        closes: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        volumes: np.ndarray,
    ) -> np.ndarray:
        """Delegate to IndicatorEngine."""
        return IndicatorEngine.compute_vwap(highs, lows, closes, volumes)

    def health_check(self) -> bool:
        """Static analyzer is always healthy — no external dependencies."""
        return True
