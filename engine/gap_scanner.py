"""
Trend-Investorr: Gap Scanner

Detects gap-up/gap-down openings and monitors for continuation or
fade patterns. gap_pct is ALREADY tracked in VolumeTracker but unused
for signal generation.

Gap Types:
    1. Gap-Up Continuation:  Gap > 1% + holds above prev close → momentum BUY
    2. Gap-Up Fade:          Gap > 3% + falls back to prev close → mean reversion SELL
    3. Gap-Down Reversal:    Gap < -1% + strong buying → reversal BUY
    4. Gap-Down Continuation: Gap < -1% + continues falling → trend SELL

Timing: Active from 9:15 AM to 11:00 AM (gap signals are morning-only).
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from loguru import logger

from core.constants import IST, MARKET_OPEN, SignalSource, SignalType
from data.models.market_data import Signal, SignalStrength, Tick
from engine.volume_scanner import VolumeTracker


class GapScanResult:
    """Result of a gap pattern detection."""

    __slots__ = (
        "symbol", "token", "pattern", "direction",
        "gap_pct", "current_price", "prev_close", "open_price",
        "strength",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        pattern: str,
        direction: str,
        gap_pct: float,
        current_price: float,
        prev_close: float,
        open_price: float,
        strength: SignalStrength,
    ):
        self.symbol = symbol
        self.token = token
        self.pattern = pattern
        self.direction = direction
        self.gap_pct = gap_pct
        self.current_price = current_price
        self.prev_close = prev_close
        self.open_price = open_price
        self.strength = strength


class GapScanner:
    """
    Gap pattern detection engine.

    Monitors gap openings and determines if they are continuation
    (momentum) or fade (mean-reversion) setups.

    Usage:
        scanner = GapScanner()
        result = scanner.evaluate(tracker, tick)
    """

    # Gap analysis is only valid in the first session
    GAP_ACTIVE_END = time(11, 0)

    def __init__(
        self,
        min_gap_pct: float = 1.0,
        large_gap_pct: float = 3.0,
        cooldown_minutes: int = 120,
    ):
        """
        Args:
            min_gap_pct: Minimum gap % to consider (abs value).
            large_gap_pct: Gap % threshold for fade patterns.
            cooldown_minutes: Cooldown per stock.
        """
        self._min_gap = min_gap_pct
        self._large_gap = large_gap_pct
        self._cooldown_minutes = cooldown_minutes

        self._cooldowns: dict[str, datetime] = {}
        self._signals_detected: int = 0

    def evaluate(
        self,
        tracker: VolumeTracker,
        tick: Tick,
    ) -> GapScanResult | None:
        """
        Evaluate gap patterns for a stock.

        Args:
            tracker: The stock's VolumeTracker (has gap_pct, prev_close).
            tick: The current tick data.

        Returns:
            GapScanResult if a tradeable gap pattern is detected.
        """
        now = tick.timestamp.time() if isinstance(tick.timestamp, datetime) else tick.timestamp

        # Only active in the morning session
        if now > self.GAP_ACTIVE_END or now < MARKET_OPEN:
            return None

        # Need gap data and prev close from tracker
        if tracker.gap_pct == 0 or tracker.prev_close <= 0 or tracker.open_price <= 0:
            return None

        gap_pct = tracker.gap_pct

        # Filter: gap must be significant
        if abs(gap_pct) < self._min_gap:
            return None

        if self._is_on_cooldown(tick.token):
            return None

        # ── Gap-Up Patterns ──
        if gap_pct > 0:
            # Gap-up continuation: price holding above previous close
            if gap_pct >= self._min_gap and tick.ltp > tracker.prev_close:
                # Strong continuation: price also above open
                if tick.ltp >= tracker.open_price:
                    return self._emit_result(
                        tracker, tick,
                        pattern="gap_up_continuation",
                        direction="bullish",
                        strength=SignalStrength.MODERATE if gap_pct < 2.0 else SignalStrength.STRONG,
                    )

            # Gap-up fade: large gap falling back toward prev close
            if gap_pct >= self._large_gap:
                # Price has come back more than halfway
                gap_size = tracker.open_price - tracker.prev_close
                retracement = tracker.open_price - tick.ltp
                if gap_size > 0 and retracement > gap_size * 0.5:
                    return self._emit_result(
                        tracker, tick,
                        pattern="gap_up_fade",
                        direction="bearish",
                        strength=SignalStrength.MODERATE,
                    )

        # ── Gap-Down Patterns ──
        if gap_pct < 0:
            # Gap-down reversal: price recovering back above open
            if abs(gap_pct) >= self._min_gap and tick.ltp > tracker.open_price:
                # Buying pressure is filling the gap
                if tracker.buy_sell_ratio > 1.3:
                    return self._emit_result(
                        tracker, tick,
                        pattern="gap_down_reversal",
                        direction="bullish",
                        strength=SignalStrength.STRONG,
                    )

            # Gap-down continuation: price keeps falling below open
            if abs(gap_pct) >= self._min_gap and tick.ltp < tracker.open_price:
                price_from_open = ((tracker.open_price - tick.ltp) / tracker.open_price) * 100
                if price_from_open > 0.5:  # Falling more than 0.5% from open
                    return self._emit_result(
                        tracker, tick,
                        pattern="gap_down_continuation",
                        direction="bearish",
                        strength=SignalStrength.MODERATE,
                    )

        return None

    def _emit_result(
        self,
        tracker: VolumeTracker,
        tick: Tick,
        pattern: str,
        direction: str,
        strength: SignalStrength,
    ) -> GapScanResult:
        self._signals_detected += 1
        self._cooldowns[tick.token] = datetime.now(IST)

        logger.info(
            f"📉 GAP: {tick.symbol} {pattern} | gap={tracker.gap_pct:+.2f}% | "
            f"open=₹{tracker.open_price:.2f} → ₹{tick.ltp:.2f} | "
            f"prev_close=₹{tracker.prev_close:.2f}"
        )

        return GapScanResult(
            symbol=tick.symbol,
            token=tick.token,
            pattern=pattern,
            direction=direction,
            gap_pct=tracker.gap_pct,
            current_price=tick.ltp,
            prev_close=tracker.prev_close,
            open_price=tracker.open_price,
            strength=strength,
        )

    def to_observation(self, result: GapScanResult) -> "Observation":
        """Convert to Observation for StrategyEngine."""
        from data.models.market_data import Observation
        confidence_map = {SignalStrength.STRONG: 0.75, SignalStrength.MODERATE: 0.60, SignalStrength.WEAK: 0.45}
        return Observation(
            symbol=result.symbol, token=result.token,
            source=SignalSource.GAP,
            direction=result.direction,
            confidence=confidence_map.get(result.strength, 0.5),
            price=result.current_price,
            pattern_name=result.pattern,
            details={"gap_pct": round(result.gap_pct, 2), "prev_close": result.prev_close, "open_price": result.open_price},
        )

    def to_signal(self, result: GapScanResult) -> Signal:
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
            source=SignalSource.GAP,
            metadata={
                "gap_pattern": result.pattern,
                "gap_pct": round(result.gap_pct, 2),
                "prev_close": result.prev_close,
                "open_price": result.open_price,
            },
        )

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        self._cooldowns.clear()
        self._signals_detected = 0
        logger.info("GapScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {"signals_detected": self._signals_detected}
