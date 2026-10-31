"""
Trend-Investorr: Delivery Scanner (Post-Market)

Post-market scanner that analyzes bhavcopy delivery data to generate
signals for the NEXT trading day. High delivery % without volume spike
is the classic "smart money" accumulation pattern.

This is a BATCH scanner — runs once per day during post_market() at ~4 PM.
Generates Signal objects with status=PENDING, stored in DB.

Signals:
    1. Stealth Accumulation: Delivery > 60% on rising price (normal volume)
    2. Dip Buying:           Delivery > 70% on declining price
    3. Delivery Spike:       Today's delivery % > 2× 30-day average
    4. Block Deal Suspect:   Delivery > 80% + single large candle

Data source: candles_1d table (with delivery_qty, delivery_pct from bhavcopy).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
from loguru import logger

from core.constants import SignalSource, SignalType
from data.models.market_data import Candle, Signal, SignalStrength


class DeliveryScanResult:
    """Result of delivery analysis for a single stock."""

    __slots__ = (
        "symbol", "token", "pattern", "direction",
        "delivery_pct", "avg_delivery_30d", "price",
        "price_change_pct", "strength",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        pattern: str,
        direction: str,
        delivery_pct: float,
        avg_delivery_30d: float,
        price: float,
        price_change_pct: float,
        strength: SignalStrength,
    ):
        self.symbol = symbol
        self.token = token
        self.pattern = pattern
        self.direction = direction
        self.delivery_pct = delivery_pct
        self.avg_delivery_30d = avg_delivery_30d
        self.price = price
        self.price_change_pct = price_change_pct
        self.strength = strength


class DeliveryScanner:
    """
    Post-market delivery analysis scanner.

    Scans all stocks' daily candles for unusual delivery patterns
    that indicate institutional activity.

    Usage:
        scanner = DeliveryScanner()
        results = scanner.scan_all(candle_cache)
        for result in results:
            signal = scanner.to_signal(result)
    """

    def __init__(
        self,
        min_delivery_pct: float = 50.0,
        high_delivery_pct: float = 60.0,
        extreme_delivery_pct: float = 80.0,
        delivery_spike_multiplier: float = 2.0,
        min_candle_days: int = 30,
    ):
        """
        Args:
            min_delivery_pct: Minimum delivery % to consider.
            high_delivery_pct: Delivery % threshold for accumulation signals.
            extreme_delivery_pct: Delivery % threshold for block deal detection.
            delivery_spike_multiplier: Multiplier vs avg for spike detection.
            min_candle_days: Minimum candles needed for avg calculation.
        """
        self._min_delivery = min_delivery_pct
        self._high_delivery = high_delivery_pct
        self._extreme_delivery = extreme_delivery_pct
        self._spike_mult = delivery_spike_multiplier
        self._min_candles = min_candle_days

    def scan_all(
        self,
        candle_cache: dict[str, list[Candle]],
    ) -> list[DeliveryScanResult]:
        """
        Scan all stocks for delivery-based signals.

        Args:
            candle_cache: token → list[Candle] (daily candles, sorted ASC).

        Returns:
            List of DeliveryScanResult for stocks showing institutional patterns.
        """
        results: list[DeliveryScanResult] = []

        for token, candles in candle_cache.items():
            if len(candles) < self._min_candles:
                continue

            result = self._analyze_stock(token, candles)
            if result:
                results.append(result)

        logger.info(
            f"DeliveryScanner: scanned {len(candle_cache)} stocks, "
            f"found {len(results)} delivery signals"
        )
        return results

    def _analyze_stock(
        self, token: str, candles: list[Candle],
    ) -> DeliveryScanResult | None:
        """Analyze a single stock for delivery patterns."""
        if not candles:
            return None

        latest = candles[-1]
        current_delivery = latest.delivery_pct

        # Skip if delivery data is missing or too low
        if current_delivery < self._min_delivery:
            return None

        # Calculate 30-day average delivery %
        delivery_values = [
            c.delivery_pct for c in candles[-30:]
            if c.delivery_pct > 0
        ]
        if len(delivery_values) < 10:
            return None

        avg_delivery = float(np.mean(delivery_values))
        if avg_delivery <= 0:
            return None

        # Calculate recent price change (5-day)
        if len(candles) >= 5:
            price_5d_ago = candles[-5].close
            price_change_pct = ((latest.close - price_5d_ago) / price_5d_ago) * 100
        else:
            price_change_pct = 0.0

        # ── Pattern Detection ──

        # 1. Block Deal Suspect: extremely high delivery
        if current_delivery >= self._extreme_delivery:
            return DeliveryScanResult(
                symbol=latest.symbol,
                token=token,
                pattern="block_deal_suspect",
                direction="bullish",
                delivery_pct=current_delivery,
                avg_delivery_30d=avg_delivery,
                price=latest.close,
                price_change_pct=price_change_pct,
                strength=SignalStrength.EXTREME,
            )

        # 2. Delivery Spike: today >> average
        delivery_ratio = current_delivery / avg_delivery
        if delivery_ratio >= self._spike_mult:
            direction = "bullish" if price_change_pct > -1.0 else "bearish"
            return DeliveryScanResult(
                symbol=latest.symbol,
                token=token,
                pattern="delivery_spike",
                direction=direction,
                delivery_pct=current_delivery,
                avg_delivery_30d=avg_delivery,
                price=latest.close,
                price_change_pct=price_change_pct,
                strength=SignalStrength.STRONG,
            )

        # 3. Stealth Accumulation: high delivery + rising price
        if current_delivery >= self._high_delivery and price_change_pct > 0:
            return DeliveryScanResult(
                symbol=latest.symbol,
                token=token,
                pattern="stealth_accumulation",
                direction="bullish",
                delivery_pct=current_delivery,
                avg_delivery_30d=avg_delivery,
                price=latest.close,
                price_change_pct=price_change_pct,
                strength=SignalStrength.MODERATE,
            )

        # 4. Dip Buying: very high delivery + declining price
        if current_delivery >= 70.0 and price_change_pct < -1.0:
            return DeliveryScanResult(
                symbol=latest.symbol,
                token=token,
                pattern="dip_buying",
                direction="bullish",
                delivery_pct=current_delivery,
                avg_delivery_30d=avg_delivery,
                price=latest.close,
                price_change_pct=price_change_pct,
                strength=SignalStrength.STRONG,
            )

        return None

    def to_signal(self, result: DeliveryScanResult) -> Signal:
        """Convert to Signal for the pipeline (next-day signal)."""
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL

        score_map = {
            SignalStrength.EXTREME: 0.80,
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
            price=result.price,
            source=SignalSource.DELIVERY,
            metadata={
                "delivery_pattern": result.pattern,
                "delivery_pct": round(result.delivery_pct, 1),
                "avg_delivery_30d": round(result.avg_delivery_30d, 1),
                "price_change_5d_pct": round(result.price_change_pct, 2),
                "next_day_signal": True,  # This is a post-market signal
            },
        )
