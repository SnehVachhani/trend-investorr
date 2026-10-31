"""
Trend-Investorr: Volume Scanner

The heart of the system — detects volume spikes in real-time by comparing
current intraday volume against historical averages (30d/90d).

How it works:
    1. Receive live tick data from WebSocket
    2. Track cumulative volume per stock throughout the day
    3. Compare against time-normalized historical averages
       (e.g., at 11:00 AM, volume should be ~30% of full-day avg)
    4. Flag stocks where volume_ratio exceeds the configurable multiplier
    5. Generate VolumeSnapshot entries for the scanner output

Time Normalization:
    A stock trading 2x its full-day average at 10:00 AM (45 min in)
    is actually at ~13x its time-normalized average, because normally
    only ~15% of daily volume happens in the first 45 minutes.

    We use a market-hour distribution curve to normalize:
        9:15-10:00  → 20% of daily volume (opening burst)
        10:00-12:00 → 25%
        12:00-14:00 → 20% (lunch lull)
        14:00-15:00 → 20%
        15:00-15:30 → 15% (closing burst)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time
from typing import Any

from loguru import logger

from core.constants import IST, MARKET_OPEN, MARKET_CLOSE
from data.models.market_data import Tick, VolumeSnapshot, SignalStrength


# Cumulative volume distribution throughout the trading day
# Key: hour, Value: cumulative % of daily volume expected by that hour
_VOLUME_CURVE = {
    9: 0.08,    # By 9:30 — ~8% (first 15 min burst)
    10: 0.20,   # By 10:00 — ~20%
    11: 0.35,   # By 11:00 — ~35%
    12: 0.48,   # By 12:00 — ~48%
    13: 0.60,   # By 1:00 PM — ~60%
    14: 0.75,   # By 2:00 PM — ~75%
    15: 0.92,   # By 3:00 PM — ~92%
    16: 1.00,   # By close — 100%
}


class VolumeTracker:
    """Tracks cumulative volume for a single stock throughout the day."""

    __slots__ = (
        "symbol", "token", "cumulative_volume", "tick_count",
        "last_price", "open_price", "high_price", "low_price",
        "last_update",
        # Newly tracked fields from tick data
        "exchange_vwap", "buy_sell_ratio", "bid_ask_spread",
        "prev_close", "gap_pct",
    )

    def __init__(self, symbol: str, token: str):
        self.symbol = symbol
        self.token = token
        self.cumulative_volume: int = 0
        self.tick_count: int = 0
        self.last_price: float = 0.0
        self.open_price: float = 0.0
        self.high_price: float = 0.0
        self.low_price: float = float("inf")
        self.last_update: datetime | None = None
        # Newly tracked
        self.exchange_vwap: float = 0.0       # avg_trade_price from tick (real exchange VWAP)
        self.buy_sell_ratio: float = 1.0      # total_buy_qty / total_sell_qty
        self.bid_ask_spread: float = 0.0      # best_ask - best_bid
        self.prev_close: float = 0.0          # previous day close
        self.gap_pct: float = 0.0             # opening gap from prev close

    def update(self, tick: Tick) -> None:
        """Update tracker with a new tick."""
        self.cumulative_volume = tick.volume  # WebSocket sends cumulative
        self.last_price = tick.ltp
        self.tick_count += 1
        self.last_update = tick.timestamp

        if tick.open > 0:
            self.open_price = tick.open
        if tick.high > 0:
            self.high_price = tick.high
        if tick.low > 0 and tick.low < self.low_price:
            self.low_price = tick.low

        # Track exchange VWAP (avg_trade_price is the real intraday VWAP)
        if tick.avg_trade_price > 0:
            self.exchange_vwap = tick.avg_trade_price

        # Track buy/sell pressure ratio
        if tick.total_sell_qty > 0:
            self.buy_sell_ratio = tick.total_buy_qty / tick.total_sell_qty

        # Track bid-ask spread
        if tick.best_bid > 0 and tick.best_ask > 0:
            self.bid_ask_spread = tick.best_ask - tick.best_bid

        # Capture previous close and opening gap (first tick only)
        if tick.close > 0 and self.prev_close == 0:
            self.prev_close = tick.close  # tick.close = previous day close
            if self.open_price > 0:
                self.gap_pct = ((self.open_price - tick.close) / tick.close) * 100

    def reset(self) -> None:
        """Reset for a new trading day."""
        self.cumulative_volume = 0
        self.tick_count = 0
        self.last_price = 0.0
        self.open_price = 0.0
        self.high_price = 0.0
        self.low_price = float("inf")
        self.last_update = None
        self.exchange_vwap = 0.0
        self.buy_sell_ratio = 1.0
        self.bid_ask_spread = 0.0
        self.prev_close = 0.0
        self.gap_pct = 0.0


class VolumeScanResult:
    """Result of scanning a single stock for volume spikes."""

    __slots__ = (
        "symbol", "token", "cumulative_volume", "avg_volume_30d",
        "avg_volume_90d", "raw_ratio", "time_normalized_ratio",
        "strength", "price", "is_spike",
        # Enriched fields from tick data
        "exchange_vwap", "buy_sell_ratio", "bid_ask_spread", "gap_pct",
    )

    def __init__(
        self,
        symbol: str,
        token: str,
        cumulative_volume: int,
        avg_volume_30d: int,
        avg_volume_90d: int,
        raw_ratio: float,
        time_normalized_ratio: float,
        strength: SignalStrength,
        price: float,
        is_spike: bool,
        exchange_vwap: float = 0.0,
        buy_sell_ratio: float = 1.0,
        bid_ask_spread: float = 0.0,
        gap_pct: float = 0.0,
    ):
        self.symbol = symbol
        self.token = token
        self.cumulative_volume = cumulative_volume
        self.avg_volume_30d = avg_volume_30d
        self.avg_volume_90d = avg_volume_90d
        self.raw_ratio = raw_ratio
        self.time_normalized_ratio = time_normalized_ratio
        self.strength = strength
        self.price = price
        self.is_spike = is_spike
        self.exchange_vwap = exchange_vwap
        self.buy_sell_ratio = buy_sell_ratio
        self.bid_ask_spread = bid_ask_spread
        self.gap_pct = gap_pct

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "volume": self.cumulative_volume,
            "avg_30d": self.avg_volume_30d,
            "raw_ratio": f"{self.raw_ratio:.1f}x",
            "normalized_ratio": f"{self.time_normalized_ratio:.1f}x",
            "strength": self.strength.value,
            "price": self.price,
            "exchange_vwap": round(self.exchange_vwap, 2),
            "buy_sell_ratio": round(self.buy_sell_ratio, 2),
            "bid_ask_spread": round(self.bid_ask_spread, 2),
            "gap_pct": round(self.gap_pct, 2),
        }


class VolumeScanner:
    """
    Real-time volume spike detection engine.

    Tracks live cumulative volume for all subscribed stocks and
    flags those exceeding the configured spike multiplier
    (time-normalized against historical averages).

    Usage:
        scanner = VolumeScanner(spike_multiplier=2.0)
        scanner.set_historical_volumes({"RELIANCE": (500000, 480000), ...})
        scanner.on_tick(tick)  # Call for each tick
        spikes = scanner.get_spikes()  # Get current spike list
    """

    def __init__(self, spike_multiplier: float = 2.0):
        """
        Args:
            spike_multiplier: Minimum time-normalized volume ratio to flag
                              as a spike. Default 2.0 = stock must be at
                              2x its expected volume for the current time.
        """
        self._spike_multiplier = spike_multiplier

        # Per-stock trackers
        self._trackers: dict[str, VolumeTracker] = {}

        # Historical averages: token -> (avg_30d, avg_90d)
        self._historical: dict[str, tuple[int, int]] = {}

        # Current spike detections
        self._spikes: dict[str, VolumeScanResult] = {}

        # Cooldown: avoid re-signaling the same stock repeatedly
        self._signal_cooldowns: dict[str, datetime] = {}

        # Stats
        self._total_ticks_processed: int = 0
        self._spikes_detected_today: int = 0

    def set_historical_volumes(self, volumes: dict[str, tuple[int, int]]) -> None:
        """
        Set historical volume averages for comparison.

        Args:
            volumes: Dict of token -> (avg_volume_30d, avg_volume_90d).
        """
        self._historical = volumes
        logger.info(f"Historical volumes loaded for {len(volumes)} stocks")

    def set_spike_multiplier(self, multiplier: float) -> None:
        """Update the spike detection threshold (runtime-tunable)."""
        self._spike_multiplier = multiplier
        logger.info(f"Volume spike multiplier updated to {multiplier}x")

    def on_tick(self, tick: Tick) -> VolumeScanResult | None:
        """
        Process a single tick and check for volume spikes.

        Called by WebSocket manager for every incoming tick.

        Args:
            tick: Real-time tick data.

        Returns:
            VolumeScanResult if a new spike is detected, None otherwise.
        """
        self._total_ticks_processed += 1

        # Get or create tracker
        if tick.token not in self._trackers:
            self._trackers[tick.token] = VolumeTracker(tick.symbol, tick.token)

        tracker = self._trackers[tick.token]
        tracker.update(tick)

        # Check for volume spike
        result = self._evaluate_volume(tracker, tick.timestamp)

        if result and result.is_spike:
            # Check cooldown
            if self._is_on_cooldown(tick.token):
                return None

            self._spikes[tick.token] = result
            self._spikes_detected_today += 1
            self._signal_cooldowns[tick.token] = tick.timestamp

            logger.info(
                f"🔥 VOLUME SPIKE: {result.symbol} | "
                f"{result.time_normalized_ratio:.1f}x normalized | "
                f"vol={result.cumulative_volume:,} vs avg={result.avg_volume_30d:,} | "
                f"₹{result.price:.2f} | {result.strength.value}"
            )
            return result

        return None

    def _evaluate_volume(
        self,
        tracker: VolumeTracker,
        current_time: datetime,
    ) -> VolumeScanResult | None:
        """
        Evaluate if a stock's current volume constitutes a spike.

        Uses time-normalized comparison against historical averages.
        """
        historical = self._historical.get(tracker.token)
        if not historical:
            return None

        avg_30d, avg_90d = historical
        if avg_30d == 0:
            return None

        # Raw ratio (current vs full-day average)
        raw_ratio = tracker.cumulative_volume / avg_30d

        # Time-normalized ratio
        expected_pct = self._get_expected_volume_pct(current_time)
        if expected_pct <= 0:
            return None

        expected_volume = avg_30d * expected_pct
        if expected_volume == 0:
            return None

        time_normalized_ratio = tracker.cumulative_volume / expected_volume

        # Determine spike strength
        is_spike = time_normalized_ratio >= self._spike_multiplier
        strength = self._determine_strength(time_normalized_ratio)

        return VolumeScanResult(
            symbol=tracker.symbol,
            token=tracker.token,
            cumulative_volume=tracker.cumulative_volume,
            avg_volume_30d=avg_30d,
            avg_volume_90d=avg_90d,
            raw_ratio=raw_ratio,
            time_normalized_ratio=time_normalized_ratio,
            strength=strength,
            price=tracker.last_price,
            is_spike=is_spike,
            exchange_vwap=tracker.exchange_vwap,
            buy_sell_ratio=tracker.buy_sell_ratio,
            bid_ask_spread=tracker.bid_ask_spread,
            gap_pct=tracker.gap_pct,
        )

    @staticmethod
    def _get_expected_volume_pct(current_time: datetime) -> float:
        """
        Get the expected cumulative volume percentage for the current time.

        Uses the volume distribution curve to determine what fraction
        of daily volume should have occurred by now.
        """
        now = current_time.time() if isinstance(current_time, datetime) else current_time

        if now < MARKET_OPEN or now > MARKET_CLOSE:
            return 1.0  # Outside market hours, use full day

        current_hour = now.hour
        current_minute = now.minute

        # Interpolate between hour boundaries
        if current_hour in _VOLUME_CURVE:
            base_pct = _VOLUME_CURVE.get(current_hour - 1, 0.0) if current_hour > 9 else 0.0
            next_pct = _VOLUME_CURVE[current_hour]
            minute_fraction = current_minute / 60.0
            return base_pct + (next_pct - base_pct) * minute_fraction

        # After market close
        return 1.0

    @staticmethod
    def _determine_strength(ratio: float) -> SignalStrength:
        """Determine signal strength from the time-normalized volume ratio."""
        if ratio >= 5.0:
            return SignalStrength.EXTREME
        elif ratio >= 3.0:
            return SignalStrength.STRONG
        elif ratio >= 2.0:
            return SignalStrength.MODERATE
        else:
            return SignalStrength.WEAK

    def _is_on_cooldown(self, token: str, cooldown_minutes: int = 30) -> bool:
        """Check if a stock is on signal cooldown to avoid spam."""
        if token not in self._signal_cooldowns:
            return False

        last_signal = self._signal_cooldowns[token]
        now = datetime.now(IST)
        elapsed = (now - last_signal).total_seconds() / 60

        return elapsed < cooldown_minutes

    # ── Accessors ────────────────────────────────────────

    def get_spikes(self) -> list[VolumeScanResult]:
        """Get all currently detected volume spikes, sorted by ratio desc."""
        spikes = list(self._spikes.values())
        spikes.sort(key=lambda s: s.time_normalized_ratio, reverse=True)
        return spikes

    def get_tracker(self, token: str) -> VolumeTracker | None:
        """Get the VolumeTracker for a specific stock (used by other scanners)."""
        return self._trackers.get(token)

    def get_snapshot(self, token: str) -> VolumeSnapshot | None:
        """Get a VolumeSnapshot for a specific stock."""
        tracker = self._trackers.get(token)
        historical = self._historical.get(token)

        if not tracker or not historical:
            return None

        avg_30d, avg_90d = historical

        return VolumeSnapshot(
            symbol=tracker.symbol,
            timestamp=tracker.last_update or datetime.now(IST),
            cumulative_volume=tracker.cumulative_volume,
            avg_volume_30d=avg_30d,
            avg_volume_90d=avg_90d,
        )

    def get_all_snapshots(self) -> list[VolumeSnapshot]:
        """Get VolumeSnapshots for all tracked stocks."""
        snapshots = []
        for token in self._trackers:
            snapshot = self.get_snapshot(token)
            if snapshot:
                snapshots.append(snapshot)
        return snapshots

    def reset_daily(self) -> None:
        """Reset all trackers for a new trading day."""
        for tracker in self._trackers.values():
            tracker.reset()
        self._spikes.clear()
        self._signal_cooldowns.clear()
        self._spikes_detected_today = 0
        logger.info("Volume scanner reset for new trading day")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "tracked_stocks": len(self._trackers),
            "historical_loaded": len(self._historical),
            "active_spikes": len(self._spikes),
            "spikes_today": self._spikes_detected_today,
            "ticks_processed": self._total_ticks_processed,
            "spike_multiplier": self._spike_multiplier,
        }

    def __repr__(self) -> str:
        return (
            f"VolumeScanner(tracking={len(self._trackers)}, "
            f"spikes={len(self._spikes)}, "
            f"multiplier={self._spike_multiplier}x)"
        )
