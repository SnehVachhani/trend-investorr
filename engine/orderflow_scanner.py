"""
Trend-Investorr: Order Flow Scanner

Detects institutional order flow imbalances using data ALREADY
tracked in VolumeTracker but barely used for signal generation.

Signals:
    1. Buy/Sell Imbalance:   buy_sell_ratio > 2.0 or < 0.5
    2. Spread Compression:   bid_ask_spread suddenly tightening → imminent move
    3. Large Bid Stacking:   total_buy_qty >> total_sell_qty → institutional demand
    4. Absorption:           Price flat despite high volume → breakout imminent

All data sources already exist in VolumeTracker — this scanner just
adds detection logic on top of the already-collected data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, Tick
from engine.volume_scanner import VolumeTracker


class OrderFlowScanResult:
    """Result of order flow analysis."""

    __slots__ = (
        "symbol", "token", "signal_name", "direction",
        "current_price", "metrics", "strength",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        signal_name: str,
        direction: str,
        current_price: float,
        metrics: dict[str, float],
        strength: SignalStrength,
    ):
        self.symbol = symbol
        self.token = token
        self.signal_name = signal_name
        self.direction = direction
        self.current_price = current_price
        self.metrics = metrics
        self.strength = strength


class OrderFlowScanner:
    """
    Order flow imbalance detection engine.

    Consumes the same VolumeTracker data that VolumeScanner uses,
    but looks at order book metrics instead of volume aggregates.

    Usage:
        scanner = OrderFlowScanner()
        result = scanner.evaluate(tracker, tick)
    """

    def __init__(
        self,
        buy_sell_ratio_threshold: float = 2.0,
        spread_compression_pct: float = 0.05,
        absorption_volume_min: float = 1.5,
        absorption_price_max_pct: float = 0.3,
        cooldown_minutes: int = 30,
    ):
        """
        Args:
            buy_sell_ratio_threshold: Min buy/sell ratio for bullish imbalance
                                      (1/threshold for bearish).
            spread_compression_pct: Tight spread threshold (% of price).
            absorption_volume_min: Min volume ratio for absorption pattern.
            absorption_price_max_pct: Max price change % for absorption
                                      (flat price + high vol).
            cooldown_minutes: Cooldown per stock.
        """
        self._bs_threshold = buy_sell_ratio_threshold
        self._spread_threshold = spread_compression_pct
        self._absorption_vol_min = absorption_volume_min
        self._absorption_price_max = absorption_price_max_pct
        self._cooldown_minutes = cooldown_minutes

        self._cooldowns: dict[str, datetime] = {}
        self._prev_spreads: dict[str, float] = {}
        self._signals_detected: int = 0

    def evaluate(
        self,
        tracker: VolumeTracker,
        tick: Tick,
        volume_ratio: float = 0.0,
    ) -> OrderFlowScanResult | None:
        """
        Evaluate order flow for a stock using its VolumeTracker state.

        Args:
            tracker: The stock's VolumeTracker (already updated with tick).
            tick: The current tick data.
            volume_ratio: Time-normalized volume ratio from VolumeScanner.

        Returns:
            OrderFlowScanResult if significant imbalance detected.
        """
        if self._is_on_cooldown(tick.token):
            return None

        # 1. Buy/Sell ratio imbalance
        result = self._check_buy_sell_imbalance(tracker, tick)
        if result:
            return result

        # 2. Spread compression (precedes breakouts)
        result = self._check_spread_compression(tracker, tick)
        if result:
            return result

        # 3. Absorption pattern (flat price + high volume)
        result = self._check_absorption(tracker, tick, volume_ratio)
        if result:
            return result

        return None

    def _check_buy_sell_imbalance(
        self, tracker: VolumeTracker, tick: Tick,
    ) -> OrderFlowScanResult | None:
        """
        Detect extreme buy/sell qty imbalances.

        buy_sell_ratio > 2.0 → strong institutional buying
        buy_sell_ratio < 0.5 → strong institutional selling
        """
        ratio = tracker.buy_sell_ratio

        if ratio >= self._bs_threshold:
            self._emit(tick.token)
            logger.info(
                f"📊 ORDER FLOW: {tick.symbol} BULLISH imbalance | "
                f"buy/sell ratio = {ratio:.2f}x | ₹{tick.ltp:.2f}"
            )
            return OrderFlowScanResult(
                symbol=tick.symbol,
                token=tick.token,
                signal_name="buy_sell_imbalance_bullish",
                direction="bullish",
                current_price=tick.ltp,
                metrics={
                    "buy_sell_ratio": round(ratio, 2),
                    "total_buy_qty": tick.total_buy_qty,
                    "total_sell_qty": tick.total_sell_qty,
                },
                strength=SignalStrength.STRONG if ratio > 3.0 else SignalStrength.MODERATE,
            )

        if ratio > 0 and ratio <= (1.0 / self._bs_threshold):
            self._emit(tick.token)
            logger.info(
                f"📊 ORDER FLOW: {tick.symbol} BEARISH imbalance | "
                f"buy/sell ratio = {ratio:.2f}x | ₹{tick.ltp:.2f}"
            )
            return OrderFlowScanResult(
                symbol=tick.symbol,
                token=tick.token,
                signal_name="buy_sell_imbalance_bearish",
                direction="bearish",
                current_price=tick.ltp,
                metrics={
                    "buy_sell_ratio": round(ratio, 2),
                    "total_buy_qty": tick.total_buy_qty,
                    "total_sell_qty": tick.total_sell_qty,
                },
                strength=SignalStrength.STRONG if ratio < 0.33 else SignalStrength.MODERATE,
            )

        return None

    def _check_spread_compression(
        self, tracker: VolumeTracker, tick: Tick,
    ) -> OrderFlowScanResult | None:
        """
        Detect bid-ask spread compression → imminent directional move.

        When the spread gets very tight, it means the market is about to
        make a decision. Combined with buy/sell bias to determine direction.
        """
        if tracker.bid_ask_spread <= 0 or tracker.exchange_vwap <= 0:
            return None

        spread_pct = (tracker.bid_ask_spread / tracker.exchange_vwap) * 100
        prev_spread = self._prev_spreads.get(tick.token, spread_pct)
        self._prev_spreads[tick.token] = spread_pct

        # Spread compressed significantly vs previous reading
        if prev_spread > 0 and spread_pct < self._spread_threshold:
            spread_compression = (prev_spread - spread_pct) / prev_spread * 100
            if spread_compression > 30:  # 30%+ compression
                # Use buy/sell ratio to determine direction
                direction = "bullish" if tracker.buy_sell_ratio > 1.0 else "bearish"
                self._emit(tick.token)

                logger.info(
                    f"📊 ORDER FLOW: {tick.symbol} spread COMPRESSED | "
                    f"spread={spread_pct:.3f}% (was {prev_spread:.3f}%) | "
                    f"direction={direction}"
                )

                return OrderFlowScanResult(
                    symbol=tick.symbol,
                    token=tick.token,
                    signal_name="spread_compression",
                    direction=direction,
                    current_price=tick.ltp,
                    metrics={
                        "spread_pct": round(spread_pct, 4),
                        "prev_spread_pct": round(prev_spread, 4),
                        "compression_pct": round(spread_compression, 1),
                        "buy_sell_ratio": round(tracker.buy_sell_ratio, 2),
                    },
                    strength=SignalStrength.WEAK,
                )

        return None

    def _check_absorption(
        self, tracker: VolumeTracker, tick: Tick, volume_ratio: float,
    ) -> OrderFlowScanResult | None:
        """
        Detect absorption: high volume but flat price → breakout imminent.

        One side (buyers or sellers) is absorbing all the selling/buying
        pressure. Once the supply/demand exhausts, price will move sharply.
        """
        if volume_ratio < self._absorption_vol_min:
            return None

        # Calculate intraday price change
        if tracker.open_price <= 0:
            return None

        price_change_pct = abs((tick.ltp - tracker.open_price) / tracker.open_price) * 100

        # High volume + flat price = absorption
        if price_change_pct < self._absorption_price_max:
            # Direction: determined by which side is absorbing
            direction = "bullish" if tracker.buy_sell_ratio > 1.2 else "bearish"

            self._emit(tick.token)
            logger.info(
                f"📊 ORDER FLOW: {tick.symbol} ABSORPTION | "
                f"vol={volume_ratio:.1f}x but price Δ={price_change_pct:.2f}% | "
                f"buy/sell={tracker.buy_sell_ratio:.2f} → {direction}"
            )

            return OrderFlowScanResult(
                symbol=tick.symbol,
                token=tick.token,
                signal_name="absorption",
                direction=direction,
                current_price=tick.ltp,
                metrics={
                    "volume_ratio": round(volume_ratio, 2),
                    "price_change_pct": round(price_change_pct, 2),
                    "buy_sell_ratio": round(tracker.buy_sell_ratio, 2),
                },
                strength=SignalStrength.MODERATE,
            )

        return None

    def to_observation(self, result: OrderFlowScanResult) -> "Observation":
        """Convert to Observation for StrategyEngine."""
        from data.models.market_data import Observation
        confidence_map = {SignalStrength.STRONG: 0.75, SignalStrength.MODERATE: 0.60, SignalStrength.WEAK: 0.45}
        return Observation(
            symbol=result.symbol, token=result.token,
            source=SignalSource.ORDER_FLOW,
            direction=result.direction,
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=result.signal_name,
            details=result.metrics,
        )

    def to_signal(self, result: OrderFlowScanResult) -> Signal:
        """Convert to Signal for the pipeline."""
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
            source=SignalSource.ORDER_FLOW,
            metadata={
                "orderflow_signal": result.signal_name,
                "direction": result.direction,
                **result.metrics,
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
        self._prev_spreads.clear()
        self._signals_detected = 0
        logger.info("OrderFlowScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {"signals_detected": self._signals_detected}
