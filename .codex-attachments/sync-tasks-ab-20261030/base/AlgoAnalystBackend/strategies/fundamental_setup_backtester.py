"""Causal 5-minute setup detection and long-only trade simulation.

A signal executes at the next 5-minute candle's open, and its base or pullback
uses only candles that have already closed. Five-minute OHLCV still does not
contain exact volume at each traded price: the POC estimate spreads each
candle's volume across its high-low interval.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from math import isfinite
from typing import Any

from core.constants import IST
from engine.filter_engine.presets import INTRADAY_BARS_PER_TRADING_DAY, get_filter_preset
from engine.swing_volume_profile import SwingVolumeProfileState, build_swing_volume_profile_states
from trading.charges import DeliveryChargeConfig

FIVE_MINUTE_BARS_PER_SESSION = INTRADAY_BARS_PER_TRADING_DAY
FUNDAMENTAL_SETUP_CANDLE_INTERVAL = "5m"
_BASE_SESSIONS = 20
_BASE_BARS = _BASE_SESSIONS * FIVE_MINUTE_BARS_PER_SESSION
_MAX_PATTERN_SESSIONS = 8
_MAX_PATTERN_BARS = _MAX_PATTERN_SESSIONS * FIVE_MINUTE_BARS_PER_SESSION
_VCP_BLOCK_BARS = 5 * FIVE_MINUTE_BARS_PER_SESSION
_52_WEEK_BARS = 252 * FIVE_MINUTE_BARS_PER_SESSION
_SMA_20_BARS = 20 * FIVE_MINUTE_BARS_PER_SESSION
_SMA_50_BARS = 50 * FIVE_MINUTE_BARS_PER_SESSION
_SMA_200_BARS = 200 * FIVE_MINUTE_BARS_PER_SESSION
_DEFAULT_MAX_HOLD_BARS = 20 * FIVE_MINUTE_BARS_PER_SESSION

SETUPS: dict[str, dict[str, str]] = {
    "consolidation_sweep_reclaim": {
        "name": "Consolidation, sweep and volume-level reclaim",
        "description": (
            "A tight 20-session range on 5-minute candles is swept below its low. "
            "Within eight sessions, a bullish 5-minute close reclaims the range's "
            "estimated high-volume price level."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the bullish reclaim closes.",
        "exit_rule": "Stop below the sweep low, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "consolidation_breakout": {
        "name": "Consolidation volume breakout",
        "description": (
            "A tight 20-session range breaks on a bullish 5-minute close at least "
            "0.5% above its high, with volume at least 1.2× the prior 20-session "
            "average for that same time of day."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the volume breakout closes.",
        "exit_rule": "Stop below the range midpoint, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "pullback_reclaim": {
        "name": "Uptrend pullback and moving-average reclaim",
        "description": (
            "In a rising trend, price pulls below its 20-session-equivalent moving "
            "average on 5-minute candles and reclaims it within eight sessions."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the bullish moving-average reclaim.",
        "exit_rule": "Stop below the pullback low, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "volatility_contraction_breakout": {
        "name": "Volatility contraction breakout (VCP-style)",
        "description": (
            "A 15-session base contracts across three successive five-session "
            "ranges measured from 5-minute bars, then breaks at least 0.5% above "
            "the base on 1.5× same-time-of-day volume."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the contraction breakout.",
        "exit_rule": "Stop below the former base high, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "high_volume_gap_continuation": {
        "name": "High-volume gap continuation",
        "description": (
            "The first 5-minute candle gaps 2–8% above the prior session close, "
            "holds near its high on 1.8× its time-of-day volume baseline, and "
            "price remains above a rising 50-session-equivalent average."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the opening gap holds.",
        "exit_rule": "Stop below the opening 5-minute candle's low, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "52_week_high_breakout": {
        "name": "52-week high breakout with volume",
        "description": (
            "A 5-minute close clears the prior 252-session-equivalent high on "
            "1.5× same-time-of-day volume, with the 50-session-equivalent "
            "average above a rising 200-session-equivalent average."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the new-high breakout.",
        "exit_rule": "Stop below the prior 52-week high, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "donchian_breakout_retest": {
        "name": "Donchian breakout and retest",
        "description": (
            "A 20-session-equivalent high breaks on above-average 5-minute volume "
            "in an established uptrend, then a 5-minute candle retests and reclaims it."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the bullish retest closes.",
        "exit_rule": "Stop below the retest low, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "trend_filtered_rsi2_rebound": {
        "name": "RSI-2 oversold rebound in an uptrend",
        "description": (
            "A 2-bar Wilder RSI on 5-minute candles recovers above 10 after an "
            "oversold reading, while session-equivalent moving averages confirm "
            "a rising long-term trend."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the bullish RSI rebound closes.",
        "exit_rule": "Stop below the recent five-session-equivalent low, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "inside_bar_trend_breakout": {
        "name": "Inside-bar breakout in a rising trend",
        "description": (
            "A bullish 5-minute close clears the mother candle after an inside "
            "5-minute bar, with a rising 50-session-equivalent average and "
            "above-average same-time-of-day volume."
        ),
        "entry_rule": "Buy at the next 5-minute candle's open after the inside-bar high breaks on a close.",
        "exit_rule": "Stop below the mother candle low, target a configurable multiple of entry risk, or exit after the configured number of 5-minute candles.",
    },
    "swing_volume_profile_pullback": {
        "name": "Bullish swing volume-profile pullback",
        "description": (
            "Requires two confirmed higher highs and higher lows. An estimated 70% "
            "volume value area is calculated across the latest swing low-to-high leg; "
            "a bullish candle must reject that area before an entry is signalled."
        ),
        "entry_rule": "Buy at the next 5-minute open after the bullish value-area rejection.",
        "exit_rule": "Stop below the latest confirmed swing low with the configured buffer, target the configured R multiple, or exit after the holding limit.",
    },
}

# These presets are shared with Watchlist Analysis and run through its
# multi-timeframe path with 5-minute execution candles and daily context.
FILTER_BACKED_SETUP_IDS = ("trend_continuation", "liquid_swing_momentum")
for _preset_id in FILTER_BACKED_SETUP_IDS:
    _preset = get_filter_preset(_preset_id)
    _holding_days = _preset.max_hold_bars // INTRADAY_BARS_PER_TRADING_DAY
    SETUPS[_preset_id] = {
        "name": _preset.name,
        "description": _preset.description,
        "entry_rule": "Enter at the next 5-minute open after the same Watchlist Analysis filters pass.",
        "exit_rule": (
            f"Use the Watchlist Analysis defaults: {_preset.default_stop_loss_pct:g}% stop, "
            f"{_preset.default_take_profit_pct:g}% target, and {_holding_days} trading-day hold limit."
        ),
    }

_MAX_BASE_WIDTH = 0.12
_POC_BINS = 24
_DEFAULT_SLIPPAGE_PCT = 0.05
_DEFAULT_TARGET_R = 2.0
_DEFAULT_STOP_BUFFER_PCT = 0.5


@dataclass(frozen=True)
class _Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class _Base:
    start_index: int
    end_index: int
    start_timestamp: datetime
    end_timestamp: datetime
    low: float
    high: float
    poc: float | None = None


@dataclass(frozen=True)
class _Signal:
    timestamp: datetime
    stop_reference: float
    poc: float | None = None
    sweep_timestamp: datetime | None = None
    base_start_timestamp: datetime | None = None
    base_end_timestamp: datetime | None = None
    swing_low: float | None = None
    swing_high: float | None = None
    value_area_low: float | None = None
    value_area_high: float | None = None


@dataclass
class _Sweep:
    base: _Base
    index: int
    timestamp: datetime
    low: float


@dataclass
class _Pullback:
    index: int
    low: float


@dataclass
class _BreakoutRetest:
    index: int
    level: float
    retest_low: float | None = None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("candle timestamp must include an ISO date and time") from exc
    else:
        raise ValueError("candle timestamp must include an ISO date and time")
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _bars(candles: list[dict[str, Any]]) -> list[_Bar]:
    result: list[_Bar] = []
    for candle in candles:
        timestamp = _timestamp(candle.get("timestamp"))
        try:
            open_ = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
            volume = float(candle["volume"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid OHLCV candle at {timestamp.isoformat()}") from exc
        if (
            not all(isfinite(value) for value in (open_, high, low, close, volume))
            or low <= 0
            or volume < 0
            or high < max(open_, close, low)
            or low > min(open_, close)
        ):
            raise ValueError(f"invalid OHLCV candle at {timestamp.isoformat()}")
        if timestamp.time() == time(0, 0) or timestamp.minute % 5 != 0 or timestamp.second or timestamp.microsecond:
            raise ValueError(f"expected a five-minute candle timestamp, got {timestamp.isoformat()}")
        result.append(_Bar(timestamp, open_, high, low, close, volume))
    result.sort(key=lambda bar: bar.timestamp)
    for previous, current in pairwise(result):
        elapsed = current.timestamp - previous.timestamp
        if elapsed <= timedelta(0):
            raise ValueError("five-minute candles must have distinct timestamps")
        if elapsed.total_seconds() % 300:
            raise ValueError("candle timestamps must align to five-minute boundaries")
    return result


def _rolling_extreme(values: list[float], period: int, *, highest: bool) -> list[float | None]:
    """Compute trailing highs/lows in linear time, including the current value."""
    result: list[float | None] = [None] * len(values)
    candidates: deque[int] = deque()
    for index, value in enumerate(values):
        first_valid = index - period + 1
        while candidates and candidates[0] < first_valid:
            candidates.popleft()
        while candidates and (
            values[candidates[-1]] <= value if highest else values[candidates[-1]] >= value
        ):
            candidates.pop()
        candidates.append(index)
        if index >= period - 1:
            result[index] = values[candidates[0]]
    return result


def _same_slot_volume_average(bars: list[_Bar], sessions: int = 20) -> list[float | None]:
    """Use only prior sessions' volume from the same five-minute time slot."""
    samples: dict[time, deque[float]] = defaultdict(deque)
    totals: dict[time, float] = defaultdict(float)
    result: list[float | None] = [None] * len(bars)
    for index, bar in enumerate(bars):
        slot = bar.timestamp.time()
        history = samples[slot]
        if len(history) == sessions:
            result[index] = totals[slot] / sessions
        history.append(bar.volume)
        totals[slot] += bar.volume
        if len(history) > sessions:
            totals[slot] -= history.popleft()
    return result


class SetupCandleSeries:
    """Validated 5-minute bars plus shared rolling features for the setups."""

    def __init__(self, candles: list[dict[str, Any]]):
        self.bars = _bars(candles)
        closes = [bar.close for bar in self.bars]
        highs = [bar.high for bar in self.bars]
        lows = [bar.low for bar in self.bars]
        volumes = [bar.volume for bar in self.bars]

        self.close_prefix = [0.0]
        self.volume_prefix = [0.0]
        for close, volume in zip(closes, volumes):
            self.close_prefix.append(self.close_prefix[-1] + close)
            self.volume_prefix.append(self.volume_prefix[-1] + volume)

        high_windows = {_BASE_BARS, _VCP_BLOCK_BARS, _52_WEEK_BARS}
        low_windows = {_BASE_BARS, _VCP_BLOCK_BARS}
        self.rolling_highs = {period: _rolling_extreme(highs, period, highest=True) for period in high_windows}
        self.rolling_lows = {period: _rolling_extreme(lows, period, highest=False) for period in low_windows}
        self.same_slot_average_volume = _same_slot_volume_average(self.bars)
        self.session_first_bar = [False] * len(self.bars)
        self.previous_session_close: list[float | None] = [None] * len(self.bars)
        last_day: date | None = None
        last_close: float | None = None
        for index, bar in enumerate(self.bars):
            session = bar.timestamp.date()
            if session != last_day:
                self.session_first_bar[index] = True
                self.previous_session_close[index] = last_close
                last_day = session
            last_close = bar.close

        self.rsi2 = _rsi_series(self.bars, 2)
        self._swing_volume_profile_states: list[SwingVolumeProfileState] | None = None

    def sma(self, index: int, period: int) -> float | None:
        if period < 1 or index < period - 1:
            return None
        return (self.close_prefix[index + 1] - self.close_prefix[index + 1 - period]) / period

    def prior_high(self, index: int, period: int) -> float | None:
        values = self.rolling_highs.get(period)
        return values[index - 1] if values is not None and index > 0 else None

    def prior_low(self, index: int, period: int) -> float | None:
        values = self.rolling_lows.get(period)
        return values[index - 1] if values is not None and index > 0 else None

    def trailing_low(self, index: int, period: int) -> float | None:
        values = self.rolling_lows.get(period)
        return values[index] if values is not None and index < len(values) else None

    def volume_sum(self, start: int, end: int) -> float:
        return self.volume_prefix[end + 1] - self.volume_prefix[start]

    def swing_volume_profile_states(self) -> list[SwingVolumeProfileState]:
        """Compute the swing profile sequence once when this setup is selected."""
        if self._swing_volume_profile_states is None:
            self._swing_volume_profile_states = build_swing_volume_profile_states(
                self.bars,
                lookback=750,
                pivot_window=5,
                value_area_percent=70,
                zone_buffer_pct=0.25,
            )
        return self._swing_volume_profile_states


def prepare_setup_candles(candles: list[dict[str, Any]]) -> SetupCandleSeries:
    """Validate and precompute a stock's candles once before testing multiple setups."""
    return SetupCandleSeries(candles)


def _estimated_poc(window: list[_Bar], low: float, high: float) -> float:
    """Approximate a high-volume level; no intrabar price-volume data exists."""
    if high <= low:
        return low
    step = (high - low) / _POC_BINS
    bins = [0.0] * _POC_BINS
    for bar in window:
        if bar.volume <= 0:
            continue
        if bar.high <= bar.low:
            index = min(_POC_BINS - 1, int((bar.close - low) / step))
            bins[max(0, index)] += bar.volume
            continue
        for index in range(_POC_BINS):
            left = low + index * step
            right = left + step
            overlap = max(0.0, min(right, bar.high) - max(left, bar.low))
            if overlap:
                bins[index] += bar.volume * overlap / (bar.high - bar.low)
    # A deterministic midpoint tie-break avoids a systematic low-bin bias.
    midpoint = (low + high) / 2
    best = max(range(_POC_BINS), key=lambda index: (bins[index], -abs(low + (index + 0.5) * step - midpoint)))
    return low + (best + 0.5) * step


def _base_before(series: SetupCandleSeries, index: int) -> _Base | None:
    if index < _BASE_BARS:
        return None
    bars = series.bars
    start_index = index - _BASE_BARS
    end_index = index - 1
    low = series.rolling_lows[_BASE_BARS][end_index]
    high = series.rolling_highs[_BASE_BARS][end_index]
    if low is None or high is None or (high - low) / low > _MAX_BASE_WIDTH:
        return None
    if series.volume_sum(start_index, end_index) <= 0:
        return None
    return _Base(
        start_index=start_index,
        end_index=end_index,
        start_timestamp=bars[start_index].timestamp,
        end_timestamp=bars[end_index].timestamp,
        low=low,
        high=high,
    )


def _sma(series: SetupCandleSeries, index: int, period: int) -> float | None:
    return series.sma(index, period)


def _rsi_series(bars: list[_Bar], period: int) -> list[float | None]:
    """Build Wilder RSI values using only closes available at each index."""
    values: list[float | None] = [None] * len(bars)
    if period < 1 or len(bars) <= period:
        return values

    gains = [0.0] * len(bars)
    losses = [0.0] * len(bars)
    for index in range(1, len(bars)):
        change = bars[index].close - bars[index - 1].close
        gains[index] = max(change, 0.0)
        losses[index] = max(-change, 0.0)

    average_gain = sum(gains[1:period + 1]) / period
    average_loss = sum(losses[1:period + 1]) / period

    def value() -> float:
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    values[period] = value()
    for index in range(period + 1, len(bars)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
        values[index] = value()
    return values


def _detect_sweep_reclaim(
    series: SetupCandleSeries, index: int, sweep: _Sweep | None,
) -> tuple[_Signal | None, _Sweep | None]:
    bars = series.bars
    bar = bars[index]
    if sweep is not None:
        age = index - sweep.index
        low = min(sweep.low, bar.low)
        if age > _MAX_PATTERN_BARS or low < sweep.base.low * 0.92:
            sweep = None
        else:
            sweep.low = low
            previous = bars[index - 1]
            poc = sweep.base.poc
            reclaimed = (
                poc is not None
                and previous.close <= poc
                and bar.close > poc
                and bar.close > sweep.base.low
                and bar.close > previous.close * 1.01
                and bar.close > bar.open
            )
            if reclaimed:
                signal = _Signal(
                    timestamp=bar.timestamp,
                    stop_reference=low,
                    poc=poc,
                    sweep_timestamp=sweep.timestamp,
                    base_start_timestamp=sweep.base.start_timestamp,
                    base_end_timestamp=sweep.base.end_timestamp,
                )
                return signal, None
            return None, sweep

    base = _base_before(series, index)
    if base is not None and base.low * 0.94 <= bar.low < base.low * 0.99 and bar.close < base.low:
        window = bars[base.start_index:base.end_index + 1]
        base = _Base(
            start_index=base.start_index,
            end_index=base.end_index,
            start_timestamp=base.start_timestamp,
            end_timestamp=base.end_timestamp,
            low=base.low,
            high=base.high,
            poc=_estimated_poc(window, base.low, base.high),
        )
        return None, _Sweep(base=base, index=index, timestamp=bar.timestamp, low=bar.low)
    return None, None


def _detect_breakout(series: SetupCandleSeries, index: int) -> _Signal | None:
    bars = series.bars
    bar = bars[index]
    base = _base_before(series, index)
    relative_volume = series.same_slot_average_volume[index]
    if base is None or index == 0 or relative_volume is None or relative_volume <= 0:
        return None
    if not (
        bars[index - 1].close <= base.high
        and bar.close > base.high * 1.005
        and bar.close > bar.open
        and bar.volume >= relative_volume * 1.2
    ):
        return None
    return _Signal(
        timestamp=bar.timestamp,
        stop_reference=(base.high + base.low) / 2,
        poc=_estimated_poc(bars[base.start_index:base.end_index + 1], base.low, base.high),
        base_start_timestamp=base.start_timestamp,
        base_end_timestamp=base.end_timestamp,
    )


def _detect_pullback_reclaim(
    series: SetupCandleSeries, index: int, pullback: _Pullback | None,
) -> tuple[_Signal | None, _Pullback | None]:
    bars = series.bars
    bar = bars[index]
    sma20 = _sma(series, index, _SMA_20_BARS)
    sma50 = _sma(series, index, _SMA_50_BARS)
    if sma20 is None or sma50 is None:
        return None, None
    previous_sma20 = _sma(series, index - 1, _SMA_20_BARS)
    trend_sma50 = _sma(series, index - 5 * FIVE_MINUTE_BARS_PER_SESSION, _SMA_50_BARS)
    trend = trend_sma50 is not None and sma20 > sma50 > trend_sma50

    if pullback is not None:
        age = index - pullback.index
        pullback.low = min(pullback.low, bar.low)
        if age > _MAX_PATTERN_BARS or bar.close < sma50 * 0.97 or not trend:
            pullback = None
        elif (
            previous_sma20 is not None
            and bars[index - 1].close <= previous_sma20
            and bar.close > sma20 * 1.002
            and bar.close > bar.open
        ):
            signal = _Signal(timestamp=bar.timestamp, stop_reference=pullback.low)
            return signal, None
        else:
            return None, pullback

    if (
        trend
        and previous_sma20 is not None
        and bars[index - 1].close >= previous_sma20
        and sma50 * 0.97 < bar.close < sma20
    ):
        return None, _Pullback(index=index, low=bar.low)
    return None, None


def _detect_volatility_contraction_breakout(series: SetupCandleSeries, index: int) -> _Signal | None:
    bars = series.bars
    bar = bars[index]
    base_bars = 15 * FIVE_MINUTE_BARS_PER_SESSION
    average_volume = series.same_slot_average_volume[index]
    if index < 20 * FIVE_MINUTE_BARS_PER_SESSION or average_volume is None or average_volume <= 0:
        return None
    block_highs = series.rolling_highs[_VCP_BLOCK_BARS]
    block_lows = series.rolling_lows[_VCP_BLOCK_BARS]
    block_end_indices = (
        index - 2 * _VCP_BLOCK_BARS - 1,
        index - _VCP_BLOCK_BARS - 1,
        index - 1,
    )
    contractions = [(block_lows[end], block_highs[end]) for end in block_end_indices]
    if any(low is None or high is None or low <= 0 for low, high in contractions):
        return None
    widths = [(high - low) / low for low, high in contractions if low is not None and high is not None]
    base_low = min(low for low, _ in contractions if low is not None)
    base_high = max(high for _, high in contractions if high is not None)
    if (
        widths[0] <= widths[1] * 1.10
        or widths[1] <= widths[2] * 1.10
        or (base_high - base_low) / base_low > 0.15
        or bar.close <= base_high * 1.005
        or bar.close <= bar.open
        or bar.volume < average_volume * 1.5
    ):
        return None
    base = bars[index - base_bars:index]
    return _Signal(
        timestamp=bar.timestamp,
        stop_reference=base_high,
        poc=_estimated_poc(base, base_low, base_high),
        base_start_timestamp=base[0].timestamp,
        base_end_timestamp=base[-1].timestamp,
    )


def _detect_high_volume_gap_continuation(series: SetupCandleSeries, index: int) -> _Signal | None:
    bars = series.bars
    bar = bars[index]
    # The rising-average check compares this 50-session average with the
    # average five sessions ago, so it needs 55 complete sessions of history.
    if (
        index < 55 * FIVE_MINUTE_BARS_PER_SESSION
        or not series.session_first_bar[index]
        or bar.timestamp.time() != time(9, 15)
    ):
        return None
    previous_close = series.previous_session_close[index]
    average_volume = series.same_slot_average_volume[index]
    sma50 = _sma(series, index, _SMA_50_BARS)
    previous_sma50 = _sma(series, index - 5 * FIVE_MINUTE_BARS_PER_SESSION, _SMA_50_BARS)
    if previous_close is None or average_volume is None:
        return None
    candle_range = bar.high - bar.low
    gap_pct = (bar.open / previous_close - 1) * 100 if previous_close > 0 else 0
    closes_near_high = candle_range > 0 and (bar.high - bar.close) / candle_range <= 0.25
    if (
        not 2 <= gap_pct <= 8
        or bar.close <= bar.open
        or not closes_near_high
        or average_volume <= 0
        or bar.volume < average_volume * 1.8
        or sma50 is None
        or previous_sma50 is None
        or sma50 <= previous_sma50
        or bar.close <= sma50
    ):
        return None
    return _Signal(timestamp=bar.timestamp, stop_reference=bar.low)


def _detect_52_week_high_breakout(series: SetupCandleSeries, index: int) -> _Signal | None:
    bar = series.bars[index]
    if index < _52_WEEK_BARS:
        return None
    prior_high = series.prior_high(index, _52_WEEK_BARS)
    average_volume = series.same_slot_average_volume[index]
    sma50 = _sma(series, index, _SMA_50_BARS)
    sma200 = _sma(series, index, _SMA_200_BARS)
    previous_sma200 = _sma(series, index - 10 * FIVE_MINUTE_BARS_PER_SESSION, _SMA_200_BARS)
    if (
        prior_high is None
        or average_volume is None
        or bar.close <= prior_high
        or bar.close <= bar.open
        or average_volume <= 0
        or bar.volume < average_volume * 1.5
        or sma50 is None
        or sma200 is None
        or previous_sma200 is None
        or sma50 <= sma200
        or sma200 <= previous_sma200
    ):
        return None
    return _Signal(timestamp=bar.timestamp, stop_reference=prior_high)


def _detect_donchian_breakout_retest(
    series: SetupCandleSeries, index: int, candidate: _BreakoutRetest | None,
) -> tuple[_Signal | None, _BreakoutRetest | None]:
    bars = series.bars
    bar = bars[index]
    if candidate is not None:
        age = index - candidate.index
        if age > _MAX_PATTERN_BARS or bar.close < candidate.level * 0.97:
            candidate = None
        else:
            touched_level = candidate.level * 0.95 <= bar.low <= candidate.level * 1.02
            if touched_level:
                candidate.retest_low = (
                    bar.low if candidate.retest_low is None
                    else min(candidate.retest_low, bar.low)
                )
            if (
                candidate.retest_low is not None
                and bar.close > candidate.level * 1.005
                and bar.close > bar.open
            ):
                return _Signal(timestamp=bar.timestamp, stop_reference=candidate.retest_low), None
            return None, candidate

    if index < 55 * FIVE_MINUTE_BARS_PER_SESSION:
        return None, None
    prior_high = series.prior_high(index, _BASE_BARS)
    average_volume = series.same_slot_average_volume[index]
    sma50 = _sma(series, index, _SMA_50_BARS)
    previous_sma50 = _sma(series, index - 5 * FIVE_MINUTE_BARS_PER_SESSION, _SMA_50_BARS)
    if (
        prior_high is not None
        and average_volume is not None
        and bar.close > prior_high * 1.005
        and bar.close > bar.open
        and average_volume > 0
        and bar.volume >= average_volume * 1.3
        and sma50 is not None
        and previous_sma50 is not None
        and bar.close > sma50
        and sma50 > previous_sma50
    ):
        return None, _BreakoutRetest(index=index, level=prior_high)
    return None, None


def _detect_trend_filtered_rsi2_rebound(series: SetupCandleSeries, index: int) -> _Signal | None:
    bars = series.bars
    if index < 219 * FIVE_MINUTE_BARS_PER_SESSION or index == 0:
        return None
    previous_rsi = series.rsi2[index - 1]
    current_rsi = series.rsi2[index]
    sma50 = _sma(series, index, _SMA_50_BARS)
    sma200 = _sma(series, index, _SMA_200_BARS)
    previous_sma200 = _sma(series, index - 20 * FIVE_MINUTE_BARS_PER_SESSION, _SMA_200_BARS)
    bar = bars[index]
    if (
        previous_rsi is None
        or current_rsi is None
        or previous_rsi > 10
        or current_rsi <= 10
        or sma50 is None
        or sma200 is None
        or previous_sma200 is None
        or sma50 <= sma200
        or sma200 <= previous_sma200
        or bar.close <= sma200
        or bar.close <= bar.open
        or bar.close <= bars[index - 1].close
    ):
        return None
    recent_low = series.trailing_low(index, 5 * FIVE_MINUTE_BARS_PER_SESSION)
    if recent_low is None:
        return None
    return _Signal(timestamp=bar.timestamp, stop_reference=recent_low)


def _detect_inside_bar_trend_breakout(series: SetupCandleSeries, index: int) -> _Signal | None:
    bars = series.bars
    if index < 55 * FIVE_MINUTE_BARS_PER_SESSION:
        return None
    mother = bars[index - 2]
    inside = bars[index - 1]
    bar = bars[index]
    average_volume = series.same_slot_average_volume[index]
    sma20 = _sma(series, index, _SMA_20_BARS)
    sma50 = _sma(series, index, _SMA_50_BARS)
    previous_sma50 = _sma(series, index - 5 * FIVE_MINUTE_BARS_PER_SESSION, _SMA_50_BARS)
    if (
        inside.high < mother.high
        and inside.low > mother.low
        and bar.close > mother.high * 1.002
        and bar.close > bar.open
        and average_volume is not None
        and average_volume > 0
        and bar.volume >= average_volume * 1.2
        and sma20 is not None
        and sma50 is not None
        and previous_sma50 is not None
        and sma20 > sma50
        and sma50 > previous_sma50
    ):
        return _Signal(timestamp=bar.timestamp, stop_reference=mother.low)
    return None


def _detect_swing_volume_profile_pullback(series: SetupCandleSeries, index: int) -> _Signal | None:
    state = series.swing_volume_profile_states()[index]
    if not state.signal or state.swing_low is None:
        return None
    return _Signal(
        timestamp=series.bars[index].timestamp,
        stop_reference=state.swing_low,
        poc=state.poc,
        swing_low=state.swing_low,
        swing_high=state.swing_high,
        value_area_low=state.value_area_low,
        value_area_high=state.value_area_high,
    )


def run_setup(
    *,
    candles: list[dict[str, Any]],
    candle_series: SetupCandleSeries | None = None,
    setup_id: str,
    start_date: date,
    end_date: date,
    initial_capital: float,
    charge_config: DeliveryChargeConfig | None = None,
    slippage_pct: float = _DEFAULT_SLIPPAGE_PCT,
    target_r: float = _DEFAULT_TARGET_R,
    max_hold_bars: int = _DEFAULT_MAX_HOLD_BARS,
    stop_buffer_pct: float = _DEFAULT_STOP_BUFFER_PCT,
    entry_signal_date_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Backtest one setup on one stock with a cash-only, whole-share wallet.

    Five-minute candle closes may create signals; only the next five-minute
    candle's open may fill them. A stop and target touched in the same candle
    resolve at the stop. Gaps through a stop fill at that candle's open. Entry
    and exit prices include adverse slippage, and NSE delivery charges are
    included in net returns by default.
    """
    if setup_id not in SETUPS:
        raise ValueError(f"unknown setup: {setup_id}")
    if setup_id in FILTER_BACKED_SETUP_IDS:
        raise ValueError(f"setup {setup_id} requires the multi-timeframe filter backtester")
    start = start_date.date() if isinstance(start_date, datetime) else start_date
    end = end_date.date() if isinstance(end_date, datetime) else end_date
    if start > end:
        raise ValueError("end_date must be on or after start_date")
    if not isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if not isfinite(slippage_pct) or not 0 <= slippage_pct < 100:
        raise ValueError("slippage_pct must be between 0 and 100")
    if isinstance(target_r, bool) or not isinstance(target_r, (int, float)) or not isfinite(target_r) or not 0.5 <= target_r <= 10:
        raise ValueError("target_r must be between 0.5 and 10")
    if isinstance(max_hold_bars, bool) or not isinstance(max_hold_bars, int) or not 1 <= max_hold_bars <= 7_500:
        raise ValueError("max_hold_bars must be an integer between 1 and 7500 five-minute candles")
    if isinstance(stop_buffer_pct, bool) or not isinstance(stop_buffer_pct, (int, float)) or not isfinite(stop_buffer_pct) or not 0 <= stop_buffer_pct <= 10:
        raise ValueError("stop_buffer_pct must be between 0 and 10")
    series = candle_series or prepare_setup_candles(candles)
    bars = series.bars
    charges = charge_config or DeliveryChargeConfig()
    buy_factor = 1 + slippage_pct / 100
    sell_factor = 1 - slippage_pct / 100
    cash = float(initial_capital)
    position: dict[str, Any] | None = None
    pending: _Signal | None = None
    sweep: _Sweep | None = None
    pullback: _Pullback | None = None
    breakout_retest: _BreakoutRetest | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []

    def close_position(price: float, timestamp: datetime, reason: str) -> None:
        nonlocal cash, position
        assert position is not None
        exit_price = price * sell_factor
        quantity = position["quantity"]
        exit_charges = charges.calculate_order("SELL", exit_price, quantity)
        cash += exit_price * quantity - exit_charges.total
        invested = position["entry_price"] * quantity + position["entry_charges"].total
        pnl = (exit_price - position["entry_price"]) * quantity - position["entry_charges"].total - exit_charges.total
        signal: _Signal = position["signal"]
        trades.append({
            "setup_id": setup_id,
            "signal_date": signal.timestamp.isoformat(),
            "entry_date": position["entry_timestamp"].isoformat(),
            "entry_price": round(position["entry_price"], 4),
            "exit_date": timestamp.isoformat(),
            "exit_price": round(exit_price, 4),
            "quantity": quantity,
            "return_pct": round(pnl / invested * 100, 4),
            "pnl": round(pnl, 2),
            "exit_reason": reason,
            "poc": round(signal.poc, 4) if signal.poc is not None else None,
            "poc_method": "five_minute_ohlcv_estimate" if signal.poc is not None else None,
            "swing_low": round(signal.swing_low, 4) if signal.swing_low is not None else None,
            "swing_high": round(signal.swing_high, 4) if signal.swing_high is not None else None,
            "value_area_low": round(signal.value_area_low, 4) if signal.value_area_low is not None else None,
            "value_area_high": round(signal.value_area_high, 4) if signal.value_area_high is not None else None,
            "stop_price": round(position["stop"], 4),
            "target_price": round(position["target"], 4),
            "target_r": target_r,
            "max_hold_bars": max_hold_bars,
            "stop_buffer_pct": stop_buffer_pct,
            "sweep_date": signal.sweep_timestamp.isoformat() if signal.sweep_timestamp else None,
            "base_start_date": signal.base_start_timestamp.isoformat() if signal.base_start_timestamp else None,
            "base_end_date": signal.base_end_timestamp.isoformat() if signal.base_end_timestamp else None,
            "entry_charges": position["entry_charges"].to_dict(),
            "exit_charges": exit_charges.to_dict(),
            "total_charges": round(position["entry_charges"].total + exit_charges.total, 2),
        })
        position = None

    for index, bar in enumerate(bars):
        session = bar.timestamp.date()
        if session > end:
            break

        entered_this_bar = False
        if pending is not None:
            if session >= start and position is None:
                entry_price = bar.open * buy_factor
                # A large overnight gap can invalidate the planned risk.
                stop = pending.stop_reference * (1 - stop_buffer_pct / 100)
                risk = entry_price - stop
                if 0 < risk <= entry_price * 0.15:
                    quantity = charges.max_buy_quantity(cash, entry_price)
                    if quantity > 0:
                        entry_charges = charges.calculate_order("BUY", entry_price, quantity)
                        cash -= entry_price * quantity + entry_charges.total
                        position = {
                            "signal": pending,
                            "entry_timestamp": bar.timestamp,
                            "entry_index": index,
                            "entry_price": entry_price,
                            "quantity": quantity,
                            "entry_charges": entry_charges,
                            "stop": stop,
                            "target": entry_price + target_r * risk,
                        }
                        entered_this_bar = True
            pending = None

        held_this_bar = position is not None
        if position is not None:
            stop = position["stop"]
            target = position["target"]
            if bar.open <= stop:
                close_position(bar.open, bar.timestamp, "gap_stop")
            elif bar.open >= target:
                close_position(bar.open, bar.timestamp, "gap_target")
            elif index - position["entry_index"] >= max_hold_bars:
                # The holding period is known before this candle begins.
                close_position(bar.open, bar.timestamp, "time_exit")
            elif bar.low <= stop:
                close_position(stop, bar.timestamp, "stop_loss")
            elif bar.high >= target:
                close_position(target, bar.timestamp, f"target_{target_r:g}r")

        if session >= start:
            if position is None:
                equity = cash
            else:
                estimated_sell_fee = charges.total_for_order("SELL", bar.close, position["quantity"])
                equity = cash + position["quantity"] * bar.close - estimated_sell_fee
            equity_curve.append({"date": bar.timestamp.isoformat(), "value": round(equity, 2)})

        # The current bar is complete only here. A position held during this
        # bar cannot also generate a fresh signal on the same close.
        if session < start or held_this_bar or entered_this_bar:
            sweep = None
            pullback = None
            breakout_retest = None
            continue
        signal: _Signal | None = None
        if setup_id == "consolidation_sweep_reclaim":
            signal, sweep = _detect_sweep_reclaim(series, index, sweep)
        elif setup_id == "consolidation_breakout":
            signal = _detect_breakout(series, index)
        elif setup_id == "pullback_reclaim":
            signal, pullback = _detect_pullback_reclaim(series, index, pullback)
        elif setup_id == "volatility_contraction_breakout":
            signal = _detect_volatility_contraction_breakout(series, index)
        elif setup_id == "high_volume_gap_continuation":
            signal = _detect_high_volume_gap_continuation(series, index)
        elif setup_id == "52_week_high_breakout":
            signal = _detect_52_week_high_breakout(series, index)
        elif setup_id == "donchian_breakout_retest":
            signal, breakout_retest = _detect_donchian_breakout_retest(series, index, breakout_retest)
        elif setup_id == "trend_filtered_rsi2_rebound":
            signal = _detect_trend_filtered_rsi2_rebound(series, index)
        elif setup_id == "inside_bar_trend_breakout":
            signal = _detect_inside_bar_trend_breakout(series, index)
        elif setup_id == "swing_volume_profile_pullback":
            signal = _detect_swing_volume_profile_pullback(series, index)
        if signal is not None and (
            entry_signal_date_map is None
            or bar.timestamp.date().isoformat() in entry_signal_date_map
        ):
            pending = signal

    if position is not None:
        final_bar = next((bar for bar in reversed(bars) if bar.timestamp.date() <= end), None)
        if final_bar is not None:
            close_position(final_bar.close, final_bar.timestamp, "end_of_test")
            if equity_curve:
                equity_curve[-1]["value"] = round(cash, 2)

    peak = float(initial_capital)
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["value"])
        max_drawdown = max(max_drawdown, (peak - point["value"]) / peak * 100)
    returns = [trade["return_pct"] for trade in trades]
    wins = sum(value > 0 for value in returns)
    metrics = {
        "total_return_pct": round((cash / initial_capital - 1) * 100, 4),
        "win_rate_pct": round(wins / len(trades) * 100, 4) if trades else 0.0,
        "average_return_pct": round(sum(returns) / len(returns), 4) if trades else 0.0,
        "max_drawdown_pct": round(max_drawdown, 4),
        "trades": len(trades),
        "final_capital": round(cash, 2),
        "total_charges": round(sum(trade["total_charges"] for trade in trades), 2),
    }
    return {
        "trades": trades,
        "metrics": metrics,
        "equity_curve": equity_curve,
        "settings": {
            "target_r": target_r,
            "max_hold_bars": max_hold_bars,
            "stop_buffer_pct": stop_buffer_pct,
            "slippage_pct": slippage_pct,
        },
    }
