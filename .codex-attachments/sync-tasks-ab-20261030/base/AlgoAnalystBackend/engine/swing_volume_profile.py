"""Causal bullish swing and volume-profile pullback calculations.

Volume at price is estimated from OHLCV candles by spreading each candle's
volume evenly across its high-low interval. A candle is only considered a
swing after ``pivot_window`` bars have closed on its right side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class SwingVolumeProfileState:
    bullish_trend: bool = False
    signal: bool = False
    swing_low: float | None = None
    swing_high: float | None = None
    swing_low_index: int | None = None
    swing_high_index: int | None = None
    poc: float | None = None
    value_area_low: float | None = None
    value_area_high: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bullish_trend": self.bullish_trend,
            "signal": self.signal,
            "swing_low": self.swing_low,
            "swing_high": self.swing_high,
            "swing_low_index": self.swing_low_index,
            "swing_high_index": self.swing_high_index,
            "poc": self.poc,
            "value_area_low": self.value_area_low,
            "value_area_high": self.value_area_high,
        }


def _field(candle: Any, name: str) -> float:
    value = candle[name] if isinstance(candle, dict) else getattr(candle, name)
    return float(value)


def _volume_profile(
    lows: Sequence[float],
    highs: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    start: int,
    end: int,
    swing_low: float,
    swing_high: float,
    bins_count: int,
    value_area_fraction: float,
) -> tuple[float, float, float]:
    span = swing_high - swing_low
    if span <= 0:
        return swing_low, swing_low, swing_low
    step = span / bins_count
    bins = [0.0] * bins_count
    for index in range(start, end + 1):
        volume = max(0.0, volumes[index])
        if volume <= 0:
            continue
        bar_low = max(swing_low, lows[index])
        bar_high = min(swing_high, highs[index])
        if bar_high <= bar_low:
            if swing_low <= closes[index] <= swing_high:
                slot = min(bins_count - 1, max(0, int((closes[index] - swing_low) / step)))
                bins[slot] += volume
            continue
        bar_range = highs[index] - lows[index]
        if bar_range <= 0:
            slot = min(bins_count - 1, max(0, int((closes[index] - swing_low) / step)))
            bins[slot] += volume
            continue
        for slot in range(bins_count):
            bin_low = swing_low + slot * step
            bin_high = bin_low + step
            overlap = max(0.0, min(bin_high, bar_high) - max(bin_low, bar_low))
            if overlap:
                bins[slot] += volume * overlap / bar_range

    midpoint_slot = (bins_count - 1) / 2
    poc_slot = max(range(bins_count), key=lambda slot: (bins[slot], -abs(slot - midpoint_slot)))
    total_volume = sum(bins)
    if total_volume <= 0:
        return (
            swing_low + (poc_slot + 0.5) * step,
            swing_low + poc_slot * step,
            swing_low + (poc_slot + 1) * step,
        )

    lower = upper = poc_slot
    accumulated = bins[poc_slot]
    target = total_volume * value_area_fraction
    while accumulated < target and (lower > 0 or upper < bins_count - 1):
        lower_volume = bins[lower - 1] if lower > 0 else -1.0
        upper_volume = bins[upper + 1] if upper < bins_count - 1 else -1.0
        if upper_volume > lower_volume:
            upper += 1
            accumulated += bins[upper]
        else:
            lower -= 1
            accumulated += bins[lower]
    return (
        swing_low + (poc_slot + 0.5) * step,
        swing_low + lower * step,
        swing_low + (upper + 1) * step,
    )


def build_swing_volume_profile_states(
    candles: Sequence[Any],
    *,
    lookback: int = 250,
    pivot_window: int = 3,
    value_area_percent: float = 70.0,
    zone_buffer_pct: float = 0.25,
    bins_count: int = 24,
) -> list[SwingVolumeProfileState]:
    """Return a point-in-time state for each candle, without using future bars."""
    count = len(candles)
    empty = SwingVolumeProfileState()
    states = [empty] * count
    if count == 0 or lookback < pivot_window * 2 + 5 or pivot_window < 1:
        return states
    if not 0 < value_area_percent <= 100:
        raise ValueError("value_area_percent must be between 0 and 100")
    if zone_buffer_pct < 0 or bins_count < 2:
        raise ValueError("zone buffer must be non-negative and at least two profile bins are required")

    opens = [_field(candle, "open") for candle in candles]
    highs = [_field(candle, "high") for candle in candles]
    lows = [_field(candle, "low") for candle in candles]
    closes = [_field(candle, "close") for candle in candles]
    volumes = [_field(candle, "volume") for candle in candles]
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    cached_key: tuple[int, int, int, int] | None = None
    cached_profile: tuple[float, float, float] | None = None

    for index in range(count):
        pivot_index = index - pivot_window
        if pivot_index >= pivot_window:
            left = pivot_index - pivot_window
            right = pivot_index + pivot_window + 1
            if highs[pivot_index] >= max(highs[left:pivot_index]) and highs[pivot_index] > max(highs[pivot_index + 1:right]):
                swing_highs.append((pivot_index, highs[pivot_index]))
            if lows[pivot_index] <= min(lows[left:pivot_index]) and lows[pivot_index] < min(lows[pivot_index + 1:right]):
                swing_lows.append((pivot_index, lows[pivot_index]))

        first_recent = index - lookback + 1
        while swing_highs and swing_highs[0][0] < first_recent:
            swing_highs.pop(0)
        while swing_lows and swing_lows[0][0] < first_recent:
            swing_lows.pop(0)
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            continue

        prior_high, latest_high = swing_highs[-2], swing_highs[-1]
        prior_low, latest_low = swing_lows[-2], swing_lows[-1]
        if not (
            latest_high[0] > latest_low[0]
            and latest_high[1] > prior_high[1]
            and latest_low[1] > prior_low[1]
            and index > latest_high[0]
            and latest_high[1] > latest_low[1]
        ):
            continue

        key = (prior_high[0], latest_high[0], prior_low[0], latest_low[0])
        if key != cached_key:
            cached_key = key
            cached_profile = _volume_profile(
                lows,
                highs,
                closes,
                volumes,
                latest_low[0],
                latest_high[0],
                latest_low[1],
                latest_high[1],
                bins_count,
                value_area_percent / 100,
            )
        if cached_profile is None:
            continue

        poc, value_area_low, value_area_high = cached_profile
        buffer = (latest_high[1] - latest_low[1]) * zone_buffer_pct / 100
        zone_low = value_area_low - buffer
        zone_high = value_area_high + buffer
        touched = lows[index] <= zone_high and highs[index] >= zone_low
        bullish_rejection = closes[index] > opens[index] and closes[index] >= zone_low and closes[index] <= zone_high
        states[index] = SwingVolumeProfileState(
            bullish_trend=True,
            signal=bool(touched and bullish_rejection),
            swing_low=latest_low[1],
            swing_high=latest_high[1],
            swing_low_index=latest_low[0],
            swing_high_index=latest_high[0],
            poc=poc,
            value_area_low=value_area_low,
            value_area_high=value_area_high,
        )
    return states

