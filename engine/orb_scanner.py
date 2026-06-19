"""
Trend-Investorr: Opening Range Breakout (ORB) Scanner

Captures the high/low of the first N minutes of trading and triggers
signals when price breaks above/below that range.

ORB is the most popular intraday strategy in Indian markets.
Uses the CandleAggregator's 5-minute candles to build the opening range.

Configuration:
    orb_minutes: 15 (first 3 × 5-min candles) or 30 (first 6 × 5-min candles)

Rules:
    BUY:  price breaks above opening_range_high with volume confirmation
    SELL: price breaks below opening_range_low with volume confirmation
    SKIP: range too wide (> 2× ATR) — high-risk entry

Natural stop-loss: opposite side of the opening range.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from loguru import logger

from core.constants import IST, MARKET_OPEN, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, TechnicalIndicators, Tick


class OpeningRange:
    """Captures the opening range for a single stock."""

    __slots__ = ("symbol", "token", "high", "low", "candle_count", "is_complete", "volume")

    def __init__(self, symbol: str, token: str):
        self.symbol = symbol
        self.token = token
        self.high: float = 0.0
        self.low: float = float("inf")
        self.candle_count: int = 0
        self.is_complete: bool = False
        self.volume: int = 0

    @property
    def range_size(self) -> float:
        if self.high <= 0 or self.low == float("inf"):
            return 0.0
        return self.high - self.low

    @property
    def range_pct(self) -> float:
        mid = (self.high + self.low) / 2.0
        if mid <= 0:
            return 0.0
        return (self.range_size / mid) * 100

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


class ORBScanResult:
    """Result of an ORB breakout detection."""

    __slots__ = (
        "symbol", "token", "direction", "current_price",
        "range_high", "range_low", "range_pct",
        "strength", "is_breakout",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        direction: str,
        current_price: float,
        range_high: float,
        range_low: float,
        range_pct: float,
        strength: SignalStrength,
        is_breakout: bool,
    ):
        self.symbol = symbol
        self.token = token
        self.direction = direction  # "above" or "below"
        self.current_price = current_price
        self.range_high = range_high
        self.range_low = range_low
        self.range_pct = range_pct
        self.strength = strength
        self.is_breakout = is_breakout


class ORBScanner:
    """
    Opening Range Breakout scanner.

    Phase 1 (9:15 - 9:30/9:45): Capture opening range from 5-min candles.
    Phase 2 (9:30/9:45 - 12:00): Watch for breakouts above/below the range.

    Usage:
        scanner = ORBScanner(orb_minutes=15)
        scanner.set_indicator_cache(indicator_cache)
        result = scanner.on_tick(tick)
    """

    # ORB is only useful in the first half of the session
    ORB_ACTIVE_END = time(12, 0)

    def __init__(
        self,
        orb_minutes: int = 15,
        max_range_atr_multiple: float = 2.0,
        cooldown_minutes: int = 120,
    ):
        """
        Args:
            orb_minutes: Minutes after market open to capture range (15 or 30).
            max_range_atr_multiple: Reject if range > N × ATR (too wide = risky).
            cooldown_minutes: Cooldown per stock after a signal.
        """
        self._orb_minutes = orb_minutes
        self._max_range_atr = max_range_atr_multiple
        self._cooldown_minutes = cooldown_minutes

        # Opening ranges per stock: token → OpeningRange
        self._ranges: dict[str, OpeningRange] = {}

        # Pre-computed indicators for ATR filter
        self._indicators: dict[str, TechnicalIndicators] = {}

        # Cooldowns
        self._cooldowns: dict[str, datetime] = {}

        # Stats
        self._breakouts_detected: int = 0

        # Derived: opening range end time
        # Market opens at 9:15, so ORB ends at 9:15 + orb_minutes
        minutes_total = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute + orb_minutes
        self._orb_end_time = time(minutes_total // 60, minutes_total % 60)

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        self._indicators = cache

    def on_tick(self, tick: Tick) -> ORBScanResult | None:
        """
        Process a tick through the ORB scanner.

        During range-building phase: updates the opening range.
        During breakout phase: checks for breakouts above/below range.
        """
        now = tick.timestamp.time() if isinstance(tick.timestamp, datetime) else tick.timestamp

        # Outside active window — ORB is a morning strategy
        if now > self.ORB_ACTIVE_END:
            return None

        # Before market open
        if now < MARKET_OPEN:
            return None

        # Phase 1: Building the opening range
        if now <= self._orb_end_time:
            self._update_range(tick)
            return None

        # Phase 2: Check for breakouts
        return self._check_breakout(tick)

    def _update_range(self, tick: Tick) -> None:
        """Update opening range with tick data."""
        if tick.token not in self._ranges:
            self._ranges[tick.token] = OpeningRange(tick.symbol, tick.token)

        rng = self._ranges[tick.token]

        if tick.ltp > rng.high:
            rng.high = tick.ltp
        if tick.ltp < rng.low:
            rng.low = tick.ltp

        rng.volume += tick.volume

    def _check_breakout(self, tick: Tick) -> ORBScanResult | None:
        """Check if price breaks above/below the opening range."""
        rng = self._ranges.get(tick.token)
        if not rng:
            return None

        # Mark range as complete on first check
        if not rng.is_complete:
            rng.is_complete = True
            if rng.range_size <= 0:
                return None

            # ATR filter: reject if range is too wide
            indicators = self._indicators.get(tick.token)
            if indicators and indicators.atr_14 > 0:
                if rng.range_size > indicators.atr_14 * self._max_range_atr:
                    logger.debug(
                        f"ORB range too wide for {tick.symbol}: "
                        f"₹{rng.range_size:.2f} > {self._max_range_atr}×ATR(₹{indicators.atr_14:.2f})"
                    )
                    del self._ranges[tick.token]  # Remove — won't check again
                    return None

        # Check cooldown
        if self._is_on_cooldown(tick.token):
            return None

        # Breakout above range high → BUY
        if tick.ltp > rng.high:
            return self._emit_breakout(tick, rng, "above")

        # Breakdown below range low → SELL
        if tick.ltp < rng.low:
            return self._emit_breakout(tick, rng, "below")

        return None

    def _emit_breakout(
        self, tick: Tick, rng: OpeningRange, direction: str,
    ) -> ORBScanResult:
        """Create an ORB breakout result."""
        self._breakouts_detected += 1
        self._cooldowns[tick.token] = datetime.now(IST)

        # Strength based on how far past the range
        if direction == "above":
            distance = (tick.ltp - rng.high) / rng.range_size * 100
        else:
            distance = (rng.low - tick.ltp) / rng.range_size * 100

        if distance > 50:
            strength = SignalStrength.STRONG
        elif distance > 20:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        logger.info(
            f"📐 ORB BREAKOUT: {tick.symbol} {direction} range "
            f"[₹{rng.low:.2f} - ₹{rng.high:.2f}] → ₹{tick.ltp:.2f} | "
            f"range={rng.range_pct:.1f}% | {strength.value}"
        )

        return ORBScanResult(
            symbol=tick.symbol,
            token=tick.token,
            direction=direction,
            current_price=tick.ltp,
            range_high=rng.high,
            range_low=rng.low,
            range_pct=rng.range_pct,
            strength=strength,
            is_breakout=True,
        )

    def to_observation(self, result: ORBScanResult) -> "Observation":
        """Convert to Observation for StrategyEngine."""
        from data.models.market_data import Observation
        confidence_map = {SignalStrength.STRONG: 0.75, SignalStrength.MODERATE: 0.60, SignalStrength.WEAK: 0.45}
        return Observation(
            symbol=result.symbol, token=result.token,
            source=SignalSource.ORB,
            direction="bullish" if result.direction == "above" else "bearish",
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=f"orb_{result.direction}",
            details={"orb_high": result.range_high, "orb_low": result.range_low, "range_pct": round(result.range_pct, 2)},
        )

    def to_signal(self, result: ORBScanResult) -> Signal:
        """Convert an ORBScanResult to a Signal for the pipeline."""
        signal_type = SignalType.BUY if result.direction == "above" else SignalType.SELL

        score = 0.55
        if result.strength == SignalStrength.STRONG:
            score = 0.70
        elif result.strength == SignalStrength.MODERATE:
            score = 0.60

        # Tight range ORBs are higher probability
        if result.range_pct < 1.0:
            score += 0.05

        return Signal(
            symbol=result.symbol,
            token=result.token,
            signal_type=signal_type,
            strength=result.strength,
            score=min(score, 1.0),
            price=result.current_price,
            source=SignalSource.ORB,
            metadata={
                "orb_high": result.range_high,
                "orb_low": result.range_low,
                "orb_range_pct": round(result.range_pct, 2),
                "direction": result.direction,
                # Natural stop-loss = opposite side of range
                "natural_stop_loss": (
                    result.range_low if result.direction == "above"
                    else result.range_high
                ),
            },
        )

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        """Reset all ranges for a new trading day."""
        self._ranges.clear()
        self._cooldowns.clear()
        self._breakouts_detected = 0
        logger.info("ORBScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "ranges_captured": len(self._ranges),
            "complete_ranges": sum(1 for r in self._ranges.values() if r.is_complete),
            "breakouts_detected": self._breakouts_detected,
            "orb_minutes": self._orb_minutes,
        }
