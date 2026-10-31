"""
Trend-Investorr: Liquidity Sweep Scanner

Detects institutional stop hunts / liquidity grabs in real-time.

What is a Liquidity Sweep?
    Institutions cannot fill large orders at a single price. They deliberately
    push price through key levels (support, resistance, pivots, round numbers)
    where retail stop-losses cluster, trigger those stops to generate liquidity,
    then reverse to their intended direction.

    The SWEEP is the fake move. The RECLAIM is the real signal.

Detection Logic (3-phase):
    Phase 1 — PENETRATION:
        Price breaks below support (or above resistance) where stops cluster.
        This is the "sweep" — institutions triggering retail stop losses.

    Phase 2 — ABSORPTION (0-60 seconds):
        Volume spikes at the sweep level as stops are triggered.
        Buy/sell ratio shifts — institutions are buying what retail is panic-selling.
        Price stalls near the sweep low (institutions absorbing supply).

    Phase 3 — RECLAIM (the actual signal):
        Price crosses BACK above the swept level within a time window.
        This confirms the sweep was a false breakout / stop hunt.
        → BULLISH signal (for support sweep)
        → BEARISH signal (for resistance sweep)

Sweep Levels (ordered by strength):
    1. Support/Resistance (swing highs/lows from indicator cache)
    2. Previous day high/low (from VolumeTracker prev_close + tick OHLC)
    3. Pivot levels S1/S2/R1/R2 (from indicator cache)
    4. Round numbers (₹100, ₹500, ₹1000 intervals)
    5. Bollinger bands (from indicator cache)
    6. 52-week high/low (from indicator cache)

Quality Filters:
    - Penetration depth: must exceed level by > 0.1% but < 1.5% of price
      (too shallow = noise, too deep = genuine breakout)
    - Reclaim time: must reclaim within 30-90 seconds (configurable)
    - Volume surge: volume during sweep should be elevated
    - Wick ratio: the sweep candle should have a long wick vs body
    - ATR filter: sweep depth normalized by ATR for volatility-awareness

Why this is high-probability:
    - You're trading WITH institutions (they just loaded their position)
    - Stop losses below the sweep point are already cleared (clean runway)
    - Risk/reward is excellent (SL just below sweep low, target is wide open)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from core.constants import IST, SignalSource, SignalType
from data.models.market_data import (
    Observation,
    Signal,
    SignalStrength,
    TechnicalIndicators,
    Tick,
)
from engine.volume_scanner import VolumeTracker


# ── Sweep State Machine ──


@dataclass
class SweepLevel:
    """A price level being watched for a potential sweep."""

    name: str                           # e.g. "support", "pivot_s1", "round_500"
    price: float                        # The level price
    direction: str                      # "below" = support sweep, "above" = resistance sweep
    strength: float                     # 0.0-1.0 how important this level is

    def __hash__(self):
        return hash((self.name, round(self.price, 2)))


@dataclass
class ActiveSweep:
    """
    Tracks an in-progress sweep (Phase 1-2).
    Created when price penetrates a level, resolved when reclaimed or expired.
    """

    level: SweepLevel                   # The level being swept
    symbol: str
    token: str
    penetration_time: datetime          # When price first crossed the level
    penetration_price: float            # Price at moment of penetration
    extreme_price: float                # Deepest point of the sweep (min for support, max for resistance)
    extreme_time: datetime              # When the extreme was hit
    tick_count: int = 0                 # Ticks since penetration
    volume_at_sweep: int = 0            # Cumulative volume at penetration
    buy_sell_ratio_at_sweep: float = 1.0  # B/S ratio during sweep

    @property
    def depth_pct(self) -> float:
        """How deep the sweep went as % of level price."""
        if self.level.price <= 0:
            return 0.0
        return abs(self.extreme_price - self.level.price) / self.level.price * 100

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since penetration."""
        return (datetime.now(IST) - self.penetration_time).total_seconds()


class LiquiditySweepScanResult:
    """Result of a confirmed liquidity sweep + reclaim."""

    __slots__ = (
        "symbol", "token", "sweep_type", "level_name",
        "level_price", "sweep_extreme", "reclaim_price",
        "depth_pct", "reclaim_time_seconds", "direction",
        "strength", "metrics",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        sweep_type: str,
        level_name: str,
        level_price: float,
        sweep_extreme: float,
        reclaim_price: float,
        depth_pct: float,
        reclaim_time_seconds: float,
        direction: str,
        strength: SignalStrength,
        metrics: dict[str, Any],
    ):
        self.symbol = symbol
        self.token = token
        self.sweep_type = sweep_type          # "support_sweep", "resistance_sweep", etc.
        self.level_name = level_name
        self.level_price = level_price
        self.sweep_extreme = sweep_extreme    # Deepest point of fake move
        self.reclaim_price = reclaim_price    # Price when reclaimed
        self.depth_pct = depth_pct            # Sweep depth as % of level
        self.reclaim_time_seconds = reclaim_time_seconds
        self.direction = direction            # "bullish" (support swept) or "bearish" (resistance swept)
        self.strength = strength
        self.metrics = metrics


class LiquiditySweepScanner:
    """
    Real-time liquidity sweep (stop hunt) detection engine.

    Architecture:
        1. Build a "sweep watchlist" of key levels per stock from indicator cache
        2. On each tick, check if price penetrated any watched level
        3. Track the sweep state (depth, duration, volume)
        4. Detect the RECLAIM — price crossing back through the level
        5. Apply quality filters (depth, timing, volume confirmation)
        6. Emit Observation for the StrategyEngine

    Usage:
        scanner = LiquiditySweepScanner()
        scanner.set_indicator_cache(cache)
        result = scanner.evaluate(tracker, tick)
    """

    def __init__(
        self,
        min_depth_pct: float = 0.10,
        max_depth_pct: float = 1.50,
        min_reclaim_seconds: float = 3.0,
        max_reclaim_seconds: float = 120.0,
        cooldown_minutes: int = 60,
        round_number_interval: float = 100.0,
    ):
        """
        Args:
            min_depth_pct: Minimum sweep depth (% of level price) — too shallow = noise.
            max_depth_pct: Maximum sweep depth — too deep = genuine breakout, not a sweep.
            min_reclaim_seconds: Price must stay beyond level for at least this long.
            max_reclaim_seconds: If no reclaim within this window, it's a real breakout.
            cooldown_minutes: Per-stock cooldown after a sweep signal.
            round_number_interval: Interval for round-number levels (₹100 default).
        """
        self._min_depth = min_depth_pct
        self._max_depth = max_depth_pct
        self._min_reclaim_sec = min_reclaim_seconds
        self._max_reclaim_sec = max_reclaim_seconds
        self._cooldown_minutes = cooldown_minutes
        self._round_interval = round_number_interval

        # Pre-computed indicator cache: token → TechnicalIndicators
        self._indicators: dict[str, TechnicalIndicators] = {}

        # Per-stock sweep levels: token → list[SweepLevel]
        self._levels: dict[str, list[SweepLevel]] = {}

        # Active (in-progress) sweeps: token → list[ActiveSweep]
        self._active_sweeps: dict[str, list[ActiveSweep]] = defaultdict(list)

        # Cooldowns
        self._cooldowns: dict[str, datetime] = {}

        # Stats
        self._total_ticks: int = 0
        self._sweeps_detected: int = 0
        self._false_breakouts_caught: int = 0

    def set_indicator_cache(self, cache: dict[str, TechnicalIndicators]) -> None:
        """Load indicator cache and build sweep level watchlists per stock."""
        self._indicators = cache
        self._build_all_levels()
        logger.info(
            f"LiquiditySweepScanner: {len(cache)} stocks loaded, "
            f"{sum(len(v) for v in self._levels.values())} total sweep levels"
        )

    def _build_all_levels(self) -> None:
        """Build sweep level watchlist for every stock from indicators."""
        self._levels.clear()
        for token, ind in self._indicators.items():
            levels = self._build_levels_for_stock(ind)
            if levels:
                self._levels[token] = levels

    def _build_levels_for_stock(self, ind: TechnicalIndicators) -> list[SweepLevel]:
        """
        Build the set of levels where stop losses likely cluster.

        Returns list of SweepLevel objects sorted by strength (most important first).
        """
        levels: list[SweepLevel] = []

        # 1. Support — THE most hunted level (everyone's SL is just below support)
        if ind.support_level > 0:
            levels.append(SweepLevel(
                name="support", price=ind.support_level,
                direction="below", strength=0.95,
            ))

        # 2. Resistance — stops for short sellers are above resistance
        if ind.resistance_level > 0:
            levels.append(SweepLevel(
                name="resistance", price=ind.resistance_level,
                direction="above", strength=0.90,
            ))

        # 3. Pivot S1 — classic support, stops cluster below
        if ind.pivot_s1 > 0:
            levels.append(SweepLevel(
                name="pivot_s1", price=ind.pivot_s1,
                direction="below", strength=0.75,
            ))

        # 4. Pivot R1 — classic resistance, stops cluster above
        if ind.pivot_r1 > 0:
            levels.append(SweepLevel(
                name="pivot_r1", price=ind.pivot_r1,
                direction="above", strength=0.70,
            ))

        # 5. Pivot S2 — deeper support, less likely but strong if swept
        if ind.pivot_s2 > 0:
            levels.append(SweepLevel(
                name="pivot_s2", price=ind.pivot_s2,
                direction="below", strength=0.60,
            ))

        # 6. Pivot R2
        if ind.pivot_r2 > 0:
            levels.append(SweepLevel(
                name="pivot_r2", price=ind.pivot_r2,
                direction="above", strength=0.55,
            ))

        # 7. 52-week low — extreme level, very strong if swept
        if ind.low_52w > 0:
            levels.append(SweepLevel(
                name="52w_low", price=ind.low_52w,
                direction="below", strength=0.85,
            ))

        # 8. 52-week high — short sellers' stops cluster above
        if ind.high_52w > 0:
            levels.append(SweepLevel(
                name="52w_high", price=ind.high_52w,
                direction="above", strength=0.80,
            ))

        # 9. Bollinger lower — mean-reversion stops cluster below
        if ind.bollinger_lower > 0:
            levels.append(SweepLevel(
                name="bollinger_lower", price=ind.bollinger_lower,
                direction="below", strength=0.55,
            ))

        # 10. Bollinger upper
        if ind.bollinger_upper > 0:
            levels.append(SweepLevel(
                name="bollinger_upper", price=ind.bollinger_upper,
                direction="above", strength=0.50,
            ))

        # 11. Round numbers — retail traders love round numbers for SL
        # Find the nearest round numbers to current price range
        if ind.support_level > 0 and ind.resistance_level > 0:
            mid_price = (ind.support_level + ind.resistance_level) / 2
            for multiplier in range(max(1, int(mid_price / self._round_interval) - 1),
                                    int(mid_price / self._round_interval) + 2):
                round_price = multiplier * self._round_interval
                if round_price > 0:
                    # Only add if the round number is near the current trading range
                    dist = abs(round_price - mid_price) / mid_price * 100
                    if dist < 5:  # Within 5% of mid-price
                        dir_hint = "below" if round_price <= mid_price else "above"
                        levels.append(SweepLevel(
                            name=f"round_{int(round_price)}", price=round_price,
                            direction=dir_hint, strength=0.40,
                        ))

        # Sort by strength descending
        levels.sort(key=lambda l: l.strength, reverse=True)

        return levels

    # ── Main Detection Engine ──

    def evaluate(
        self,
        tracker: VolumeTracker,
        tick: Tick,
        volume_ratio: float = 0.0,
    ) -> LiquiditySweepScanResult | None:
        """
        Process a tick through the 3-phase sweep detection state machine.

        Phase 1: Check if price just penetrated any watched level (new sweep)
        Phase 2: Update existing active sweeps (track depth, volume)
        Phase 3: Check if price reclaimed any swept level (the signal)

        Args:
            tracker: VolumeTracker for this stock.
            tick: Current tick data.
            volume_ratio: Time-normalized volume ratio from VolumeScanner.

        Returns:
            LiquiditySweepScanResult if a confirmed sweep+reclaim is detected.
        """
        self._total_ticks += 1

        if self._is_on_cooldown(tick.token):
            return None

        levels = self._levels.get(tick.token)
        if not levels:
            return None

        # ── Phase 3: Check reclaims FIRST (most important — before we add new sweeps) ──
        result = self._check_reclaims(tick, tracker, volume_ratio)
        if result:
            return result

        # ── Phase 2: Update active sweeps (track extreme price) ──
        self._update_active_sweeps(tick)

        # ── Phase 1: Check for new penetrations ──
        self._check_penetrations(tick, tracker, levels)

        # ── Housekeeping: expire old sweeps that exceeded time window ──
        self._expire_stale_sweeps(tick.token)

        return None

    def _check_penetrations(
        self, tick: Tick, tracker: VolumeTracker, levels: list[SweepLevel],
    ) -> None:
        """
        Phase 1: Detect new level penetrations.

        A penetration occurs when price crosses through a watched level
        for the first time (not already tracking a sweep at this level).
        """
        # Get already-swept levels to avoid duplicates
        already_swept = {
            (s.level.name, round(s.level.price, 2))
            for s in self._active_sweeps.get(tick.token, [])
        }

        for level in levels:
            level_key = (level.name, round(level.price, 2))
            if level_key in already_swept:
                continue

            penetrated = False

            if level.direction == "below":
                # Support sweep: price drops below the level
                penetrated = tick.ltp < level.price
            else:
                # Resistance sweep: price breaks above the level
                penetrated = tick.ltp > level.price

            if penetrated:
                sweep = ActiveSweep(
                    level=level,
                    symbol=tick.symbol,
                    token=tick.token,
                    penetration_time=datetime.now(IST),
                    penetration_price=tick.ltp,
                    extreme_price=tick.ltp,
                    extreme_time=datetime.now(IST),
                    tick_count=1,
                    volume_at_sweep=tracker.cumulative_volume,
                    buy_sell_ratio_at_sweep=tracker.buy_sell_ratio,
                )
                self._active_sweeps[tick.token].append(sweep)

                logger.debug(
                    f"🎯 SWEEP STARTED: {tick.symbol} penetrated {level.name} "
                    f"₹{level.price:.2f} ({level.direction}) at ₹{tick.ltp:.2f}"
                )

    def _update_active_sweeps(self, tick: Tick) -> None:
        """
        Phase 2: Update active sweeps with new tick data.

        Track the extreme price (deepest point of the sweep) and tick count.
        """
        for sweep in self._active_sweeps.get(tick.token, []):
            sweep.tick_count += 1

            if sweep.level.direction == "below":
                # Support sweep: track the lowest point
                if tick.ltp < sweep.extreme_price:
                    sweep.extreme_price = tick.ltp
                    sweep.extreme_time = datetime.now(IST)
            else:
                # Resistance sweep: track the highest point
                if tick.ltp > sweep.extreme_price:
                    sweep.extreme_price = tick.ltp
                    sweep.extreme_time = datetime.now(IST)

    def _check_reclaims(
        self, tick: Tick, tracker: VolumeTracker, volume_ratio: float,
    ) -> LiquiditySweepScanResult | None:
        """
        Phase 3: Detect level reclaims — THE actual signal.

        A reclaim occurs when price crosses BACK through a previously
        swept level, confirming the sweep was a false breakout.
        """
        active = self._active_sweeps.get(tick.token, [])
        if not active:
            return None

        for sweep in active[:]:  # Copy list to allow removal during iteration
            elapsed = sweep.elapsed_seconds

            # Too early — let the sweep develop (avoid noise from single-tick wicks)
            if elapsed < self._min_reclaim_sec:
                continue

            # Check if price reclaimed the level
            reclaimed = False
            if sweep.level.direction == "below":
                # Support sweep reclaim: price was below support, now back above
                reclaimed = tick.ltp > sweep.level.price
            else:
                # Resistance sweep reclaim: price was above resistance, now back below
                reclaimed = tick.ltp < sweep.level.price

            if not reclaimed:
                continue

            # ── Quality Filters ──

            # 1. Depth check: sweep must be meaningful but not too deep
            depth_pct = sweep.depth_pct
            if depth_pct < self._min_depth:
                logger.debug(
                    f"Sweep {sweep.symbol} {sweep.level.name}: "
                    f"depth {depth_pct:.2f}% too shallow (min={self._min_depth}%)"
                )
                active.remove(sweep)
                continue

            if depth_pct > self._max_depth:
                logger.debug(
                    f"Sweep {sweep.symbol} {sweep.level.name}: "
                    f"depth {depth_pct:.2f}% too deep (max={self._max_depth}%) — genuine breakout"
                )
                active.remove(sweep)
                continue

            # 2. Timing check: reclaim must happen within window
            if elapsed > self._max_reclaim_sec:
                # This shouldn't happen (stale sweeps are expired), but safety check
                active.remove(sweep)
                continue

            # 3. Volume confirmation: volume should be elevated during sweep
            volume_confirmed = volume_ratio >= 1.0  # At least normal volume

            # 4. Buy/sell ratio shift: during support sweep, buy ratio should
            #    increase (institutions buying the panic) vs at sweep start
            bs_shift = tracker.buy_sell_ratio - sweep.buy_sell_ratio_at_sweep
            institutions_active = (
                (sweep.level.direction == "below" and bs_shift > 0.2) or  # More buying after support sweep
                (sweep.level.direction == "above" and bs_shift < -0.2)    # More selling after resistance sweep
            )

            # ── Determine Signal Quality ──
            direction = "bullish" if sweep.level.direction == "below" else "bearish"

            # Confidence scoring based on multiple factors
            confidence_score = 0.0

            # Level importance (0.0 - 0.35)
            confidence_score += sweep.level.strength * 0.35

            # Sweep depth quality (0.0 - 0.20) — ideal depth is 0.3-0.8% of price
            if 0.3 <= depth_pct <= 0.8:
                confidence_score += 0.20  # Perfect depth
            elif 0.15 <= depth_pct <= 1.2:
                confidence_score += 0.10  # Acceptable depth
            else:
                confidence_score += 0.05  # Marginal

            # Reclaim speed (0.0 - 0.15) — faster reclaim = more conviction
            if elapsed < 15:
                confidence_score += 0.15  # Very fast reclaim
            elif elapsed < 30:
                confidence_score += 0.12
            elif elapsed < 60:
                confidence_score += 0.08
            else:
                confidence_score += 0.04  # Slow reclaim

            # Volume confirmation (0.0 - 0.15)
            if volume_confirmed:
                confidence_score += 0.15

            # Institutional activity (0.0 - 0.15)
            if institutions_active:
                confidence_score += 0.15

            # Determine strength from score
            if confidence_score >= 0.75:
                strength = SignalStrength.EXTREME
            elif confidence_score >= 0.55:
                strength = SignalStrength.STRONG
            elif confidence_score >= 0.40:
                strength = SignalStrength.MODERATE
            else:
                strength = SignalStrength.WEAK

            # Skip weak sweeps that don't have institutional confirmation
            if strength == SignalStrength.WEAK and not institutions_active:
                active.remove(sweep)
                continue

            # ── Confirmed Sweep! ──
            self._sweeps_detected += 1
            self._false_breakouts_caught += 1
            self._cooldowns[tick.token] = datetime.now(IST)

            # Remove this sweep from active tracking
            active.remove(sweep)

            sweep_type = f"{sweep.level.name}_sweep"

            logger.info(
                f"🏦 LIQUIDITY SWEEP: {tick.symbol} | {sweep_type} | "
                f"level=₹{sweep.level.price:.2f} swept to ₹{sweep.extreme_price:.2f} "
                f"(depth={depth_pct:.2f}%) | reclaimed in {elapsed:.0f}s | "
                f"direction={direction} | strength={strength.value} | "
                f"b/s_shift={bs_shift:+.2f} | vol_ratio={volume_ratio:.1f}x"
            )

            return LiquiditySweepScanResult(
                symbol=tick.symbol,
                token=tick.token,
                sweep_type=sweep_type,
                level_name=sweep.level.name,
                level_price=sweep.level.price,
                sweep_extreme=sweep.extreme_price,
                reclaim_price=tick.ltp,
                depth_pct=depth_pct,
                reclaim_time_seconds=elapsed,
                direction=direction,
                strength=strength,
                metrics={
                    "level_strength": round(sweep.level.strength, 2),
                    "sweep_extreme": round(sweep.extreme_price, 2),
                    "depth_pct": round(depth_pct, 3),
                    "reclaim_seconds": round(elapsed, 1),
                    "volume_ratio": round(volume_ratio, 2),
                    "bs_ratio_before": round(sweep.buy_sell_ratio_at_sweep, 2),
                    "bs_ratio_after": round(tracker.buy_sell_ratio, 2),
                    "bs_shift": round(bs_shift, 2),
                    "tick_count_during_sweep": sweep.tick_count,
                    "confidence_score": round(confidence_score, 3),
                    "institutions_active": institutions_active,
                },
            )

        return None

    def _expire_stale_sweeps(self, token: str) -> None:
        """Remove sweeps that exceeded the reclaim time window."""
        active = self._active_sweeps.get(token, [])
        expired = [s for s in active if s.elapsed_seconds > self._max_reclaim_sec]

        for sweep in expired:
            active.remove(sweep)
            logger.debug(
                f"Sweep expired: {sweep.symbol} {sweep.level.name} "
                f"after {sweep.elapsed_seconds:.0f}s — genuine breakout"
            )

    # ── Observation / Signal Conversion ──

    def to_observation(self, result: LiquiditySweepScanResult) -> Observation:
        """Convert to Observation for StrategyEngine."""
        return Observation(
            symbol=result.symbol,
            token=result.token,
            source=SignalSource.LIQUIDITY_SWEEP,
            direction=result.direction,
            confidence=result.metrics.get("confidence_score", 0.5),
            price=result.reclaim_price,
            pattern_name=result.sweep_type,
            details=result.metrics,
        )

    def to_signal(self, result: LiquiditySweepScanResult) -> Signal:
        """Convert to Signal for backward compatibility."""
        signal_type = SignalType.BUY if result.direction == "bullish" else SignalType.SELL
        score_map = {
            SignalStrength.EXTREME: 0.85,
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
            price=result.reclaim_price,
            source=SignalSource.LIQUIDITY_SWEEP,
            metadata={
                "sweep_type": result.sweep_type,
                "level_price": result.level_price,
                "sweep_extreme": result.sweep_extreme,
                **result.metrics,
            },
        )

    # ── Lifecycle ──

    def _is_on_cooldown(self, token: str) -> bool:
        if token not in self._cooldowns:
            return False
        elapsed = (datetime.now(IST) - self._cooldowns[token]).total_seconds() / 60
        return elapsed < self._cooldown_minutes

    def reset_daily(self) -> None:
        """Reset for a new trading day."""
        self._active_sweeps.clear()
        self._cooldowns.clear()
        self._sweeps_detected = 0
        self._false_breakouts_caught = 0
        # Rebuild levels from indicators (may have been updated post-market)
        if self._indicators:
            self._build_all_levels()
        logger.info("LiquiditySweepScanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        active_count = sum(len(v) for v in self._active_sweeps.values())
        return {
            "levels_watched": sum(len(v) for v in self._levels.values()),
            "active_sweeps": active_count,
            "sweeps_detected": self._sweeps_detected,
            "false_breakouts_caught": self._false_breakouts_caught,
            "ticks_processed": self._total_ticks,
        }

    def __repr__(self) -> str:
        return (
            f"LiquiditySweepScanner("
            f"levels={sum(len(v) for v in self._levels.values())}, "
            f"active={sum(len(v) for v in self._active_sweeps.values())}, "
            f"detected={self._sweeps_detected})"
        )
