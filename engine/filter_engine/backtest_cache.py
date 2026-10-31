"""Shared, point-in-time atomic calculations for filter backtest batches.

The normal evaluator intentionally favors explainability and accepts a small
series for every candle. Running many presets through that evaluator would
repeat the same indicator work many times.  This module computes the
deterministic OHLCV conditions once per unique condition signature and keeps
the resulting values in compact, timestamp-aligned series.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import numpy as np

from .catalog import FilterCatalog
from .evaluator import FilterEvaluator
from .models import (
    CandlePoint,
    Direction,
    FilterCondition,
    FilterExpression,
    FilterGroup,
    RuleNode,
    Timeframe,
)
from engine.swing_volume_profile import build_swing_volume_profile_states

AtomicResult = tuple[bool, Any, Any, str, Direction]


@dataclass
class _CachedSeries:
    timestamps: list[datetime]
    values: list[AtomicResult | None]


class AtomicBacktestCache:
    """Timestamp-indexed cache shared by all presets in one batch."""

    def __init__(self) -> None:
        self._series: dict[tuple[str, int], _CachedSeries] = {}

    def add(self, key: str, context_length: int, timestamps: list[datetime], values: list[AtomicResult | None]) -> None:
        self._series[(key, context_length)] = _CachedSeries(timestamps=timestamps, values=values)

    def get(self, key: str, timestamp: datetime | None, context_length: int) -> AtomicResult | None:
        if timestamp is None:
            return None
        series = self._series.get((key, context_length))
        if series is None:
            return None
        index = bisect_right(series.timestamps, timestamp) - 1
        if index < 0 or index >= len(series.values):
            return None
        return series.values[index]

    def get_series(self, key: str, context_length: int) -> _CachedSeries | None:
        """Return a prepared series for a compiled backtest expression.

        The normal evaluator continues to use ``get`` for explainable,
        point-in-time evaluation.  This accessor is deliberately read-only so
        the high-throughput backtest path can combine the exact same cached
        atomic values without rebuilding indicator inputs on every bar.
        """
        return self._series.get((key, context_length))

    @property
    def series_count(self) -> int:
        return len(self._series)


def _windows(values: np.ndarray, length: int) -> np.ndarray | None:
    if length <= 0 or len(values) < length:
        return None
    return np.lib.stride_tricks.sliding_window_view(values, length)


def _ema_series(windows: np.ndarray, period: int) -> np.ndarray:
    """Compute the same seeded EMA as IndicatorEngine for every row."""
    result = np.empty_like(windows, dtype=float)
    seed = np.mean(windows[:, :period], axis=1)
    result[:, :period] = seed[:, None]
    result[:, period - 1] = seed
    multiplier = 2.0 / (period + 1)
    previous = seed
    for index in range(period, windows.shape[1]):
        previous = windows[:, index] * multiplier + previous * (1 - multiplier)
        result[:, index] = previous
    return result


def _ema_last(windows: np.ndarray, period: int) -> np.ndarray:
    return _ema_series(windows, period)[:, -1]


def _rsi_last(windows: np.ndarray, period: int) -> np.ndarray:
    deltas = np.diff(windows, axis=1)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    average_gain = np.mean(gains[:, :period], axis=1)
    average_loss = np.mean(losses[:, :period], axis=1)
    for index in range(period, gains.shape[1]):
        average_gain = (average_gain * (period - 1) + gains[:, index]) / period
        average_loss = (average_loss * (period - 1) + losses[:, index]) / period
    rs = np.divide(
        average_gain,
        average_loss,
        out=np.full_like(average_gain, np.inf),
        where=average_loss != 0,
    )
    result = 100 - (100 / (1 + rs))
    result[(average_gain == 0) & (average_loss == 0)] = 50.0
    return result


def _atr_last(high_windows: np.ndarray, low_windows: np.ndarray, close_windows: np.ndarray, period: int) -> np.ndarray:
    true_range = np.maximum(
        high_windows[:, 1:] - low_windows[:, 1:],
        np.maximum(
            np.abs(high_windows[:, 1:] - close_windows[:, :-1]),
            np.abs(low_windows[:, 1:] - close_windows[:, :-1]),
        ),
    )
    result = np.zeros((true_range.shape[0], high_windows.shape[1]), dtype=float)
    if true_range.shape[1] < period:
        result[:, 1:] = true_range
        return result[:, -1]
    result[:, period] = np.mean(true_range[:, :period], axis=1)
    for index in range(period + 1, high_windows.shape[1]):
        result[:, index] = (
            result[:, index - 1] * (period - 1) + true_range[:, index - 1]
        ) / period
    result[:, :period] = result[:, period, None]
    return result[:, -1]


def _adx_last(high_windows: np.ndarray, low_windows: np.ndarray, close_windows: np.ndarray, period: int) -> np.ndarray:
    count = high_windows.shape[0]
    length = high_windows.shape[1]
    if length < period * 2:
        return np.zeros(count, dtype=float)

    up_move = high_windows[:, 1:] - high_windows[:, :-1]
    down_move = low_windows[:, :-1] - low_windows[:, 1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = np.zeros((count, length), dtype=float)
    true_range = np.maximum(
        high_windows[:, 1:] - low_windows[:, 1:],
        np.maximum(
            np.abs(high_windows[:, 1:] - close_windows[:, :-1]),
            np.abs(low_windows[:, 1:] - close_windows[:, :-1]),
        ),
    )
    atr[:, period] = np.mean(true_range[:, :period], axis=1)
    for index in range(period + 1, length):
        atr[:, index] = (atr[:, index - 1] * (period - 1) + true_range[:, index - 1]) / period
    atr[:, :period] = atr[:, period, None]

    smooth_plus = np.zeros_like(plus_dm)
    smooth_minus = np.zeros_like(minus_dm)
    smooth_plus[:, period - 1] = np.sum(plus_dm[:, :period], axis=1)
    smooth_minus[:, period - 1] = np.sum(minus_dm[:, :period], axis=1)
    for index in range(period, plus_dm.shape[1]):
        smooth_plus[:, index] = smooth_plus[:, index - 1] - smooth_plus[:, index - 1] / period + plus_dm[:, index]
        smooth_minus[:, index] = smooth_minus[:, index - 1] - smooth_minus[:, index - 1] / period + minus_dm[:, index]

    atr_trimmed = atr[:, 1:]
    plus_di = np.divide(smooth_plus * 100, atr_trimmed, out=np.zeros_like(smooth_plus), where=atr_trimmed != 0)
    minus_di = np.divide(smooth_minus * 100, atr_trimmed, out=np.zeros_like(smooth_minus), where=atr_trimmed != 0)
    di_sum = plus_di + minus_di
    dx = np.divide(np.abs(plus_di - minus_di) * 100, di_sum, out=np.zeros_like(di_sum), where=di_sum != 0)

    adx = np.zeros_like(dx)
    if dx.shape[1] >= period * 2:
        adx[:, period * 2 - 2] = np.mean(dx[:, period - 1: period * 2 - 1], axis=1)
        for index in range(period * 2 - 1, dx.shape[1]):
            adx[:, index] = (adx[:, index - 1] * (period - 1) + dx[:, index]) / period
    return adx[:, -1]


def _collect_conditions(node: RuleNode, output: list[FilterCondition]) -> None:
    if isinstance(node, FilterCondition):
        output.append(node)
        return
    if isinstance(node, FilterGroup):
        for child in node.children:
            _collect_conditions(child, output)


def _condition_result_series(
    condition: FilterCondition,
    points: list[CandlePoint],
    catalog: FilterCatalog,
    context_length: int,
) -> list[AtomicResult | None] | None:
    """Calculate one condition across a complete timeframe series."""
    requirement = catalog.requirements_for(
        condition.filter_id,
        condition.params,
        timeframe=condition.timeframe,
    )
    length = max(context_length, requirement.total_candles)
    if condition.filter_id == "data_completeness":
        length = max(length, int(condition.params.get("minimum_candles", 30)))
    if len(points) < length:
        return [None] * len(points)

    if condition.filter_id == "bullish_swing_poc_pullback":
        states = build_swing_volume_profile_states(
            points,
            lookback=int(condition.params.get("lookback", 250)),
            pivot_window=int(condition.params.get("pivot_window", 3)),
            value_area_percent=float(condition.params.get("value_area_percent", 70)),
            zone_buffer_pct=float(condition.params.get("zone_buffer_pct", 0.25)),
        )
        values: list[AtomicResult | None] = [None] * len(points)
        value_area_percent = float(condition.params.get("value_area_percent", 70))
        for index in range(length - 1, len(points)):
            state = states[index]
            values[index] = (
                state.signal,
                state.as_dict(),
                {"value_area_percent": value_area_percent},
                "bullish swing pullback rejected from the volume-profile value area"
                if state.signal
                else "no confirmed bullish swing pullback rejection at the volume-profile value area",
                Direction.BULLISH if state.bullish_trend else Direction.NEUTRAL,
            )
        return values

    closes = np.asarray([point.close for point in points], dtype=float)
    highs = np.asarray([point.high for point in points], dtype=float)
    lows = np.asarray([point.low for point in points], dtype=float)
    volumes = np.asarray([point.volume for point in points], dtype=float)
    close_windows = _windows(closes, length)
    high_windows = _windows(highs, length)
    low_windows = _windows(lows, length)
    volume_windows = _windows(volumes, length)
    if close_windows is None or high_windows is None or low_windows is None or volume_windows is None:
        return [None] * len(points)

    filter_id = condition.filter_id
    params = condition.params
    result: list[AtomicResult | None] = [None] * len(points)
    latest = close_windows[:, -1]
    if filter_id == "data_completeness":
        minimum = int(params.get("minimum_candles", 30))
        values = [(True, length, minimum, f"received {length} candles", Direction.NEUTRAL)] * close_windows.shape[0]
    elif filter_id == "price_range":
        minimum = params.get("min_price")
        maximum = params.get("max_price")
        passed = np.array([(minimum is None or value >= minimum) and (maximum is None or value <= maximum) for value in latest])
        values = [(bool(ok), float(value), {"min": minimum, "max": maximum}, f"close={float(value):.2f}", Direction.NEUTRAL) for ok, value in zip(passed, latest)]
    elif filter_id == "average_volume":
        period = int(params.get("period", 30))
        averages = np.mean(volume_windows[:, -period:], axis=1)
        threshold = float(params.get("minimum", 0))
        values = [(float(actual) >= threshold, float(actual), threshold, f"average volume={float(actual):.0f}", Direction.NEUTRAL) for actual in averages]
    elif filter_id == "average_traded_value":
        period = int(params.get("period", 30))
        averages = np.mean(close_windows[:, -period:] * volume_windows[:, -period:], axis=1)
        threshold = float(params.get("minimum", 0))
        values = [(float(actual) >= threshold, float(actual), threshold, f"average traded value={float(actual):.2f}", Direction.NEUTRAL) for actual in averages]
    elif filter_id == "price_above_sma":
        period = int(params.get("period", 200))
        averages = np.mean(close_windows[:, -period:], axis=1)
        relation = params.get("relation", "above")
        passed = latest > averages if relation == "above" else latest < averages
        direction = Direction.BULLISH if relation == "above" else Direction.BEARISH
        values = [(bool(ok), float(value), float(average), f"close {relation} SMA({period})", direction) for ok, value, average in zip(passed, latest, averages)]
    elif filter_id == "ema_relation":
        fast_period = int(params.get("fast_period", 20))
        slow_period = int(params.get("slow_period", 50))
        fast = _ema_last(close_windows, fast_period)
        slow = _ema_last(close_windows, slow_period)
        relation = params.get("relation", "above")
        passed = fast > slow if relation == "above" else fast < slow
        direction = Direction.BULLISH if relation == "above" else Direction.BEARISH
        values = [(bool(ok), {"fast": float(f), "slow": float(s)}, relation, f"EMA({fast_period}) {relation} EMA({slow_period})", direction) for ok, f, s in zip(passed, fast, slow)]
    elif filter_id == "rsi_range":
        period = int(params.get("period", 14))
        rsi = _rsi_last(close_windows, period)
        minimum = float(params.get("minimum", 40))
        maximum = float(params.get("maximum", 70))
        values = [(minimum <= float(actual) <= maximum, float(actual), {"min": minimum, "max": maximum}, f"RSI={float(actual):.2f}", Direction.NEUTRAL) for actual in rsi]
    elif filter_id == "macd_direction":
        fast_series = _ema_series(close_windows, 12)
        slow_series = _ema_series(close_windows, 26)
        line_series = fast_series - slow_series
        signal = _ema_last(line_series, 9)
        line = line_series[:, -1]
        histogram = line - signal
        direction = params.get("direction", "bullish")
        bullish = line > signal
        bearish = line < signal
        passed = bullish if direction == "bullish" else bearish
        values = [
            (
                bool(ok),
                {"line": float(line_value), "signal": float(signal_value), "histogram": float(histogram_value)},
                direction,
                f"MACD direction={direction}",
                Direction.BULLISH if is_bullish else Direction.BEARISH if is_bearish else Direction.NEUTRAL,
            )
            for ok, line_value, signal_value, histogram_value, is_bullish, is_bearish in zip(passed, line, signal, histogram, bullish, bearish)
        ]
    elif filter_id == "atr_percent":
        period = int(params.get("period", 14))
        atr = _atr_last(high_windows, low_windows, close_windows, period)
        actual = np.divide(atr * 100, latest, out=np.zeros_like(atr), where=latest != 0)
        minimum = float(params.get("minimum", 0))
        maximum = float(params.get("maximum", 10))
        values = [(minimum <= float(value) <= maximum, float(value), {"min": minimum, "max": maximum}, f"ATR%={float(value):.2f}", Direction.NEUTRAL) for value in actual]
    elif filter_id == "adx_threshold":
        period = int(params.get("period", 14))
        adx = _adx_last(high_windows, low_windows, close_windows, period)
        threshold = float(params.get("minimum", 20))
        values = [(float(actual) >= threshold, float(actual), threshold, f"ADX={float(actual):.2f}", Direction.NEUTRAL) for actual in adx]
    elif filter_id == "relative_volume":
        period = int(params.get("period", 20))
        previous = volume_windows[:, -period - 1:-1]
        averages = np.mean(previous, axis=1)
        actual = np.divide(volume_windows[:, -1], averages, out=np.zeros_like(averages), where=averages != 0)
        threshold = float(params.get("multiple", 1.5))
        values = [(float(value) >= threshold, float(value), threshold, f"relative volume={float(value):.2f}x", Direction.NEUTRAL) for value in actual]
    elif filter_id == "previous_level":
        level = params.get("level", "high")
        previous = {"high": high_windows[:, -2], "low": low_windows[:, -2], "close": close_windows[:, -2]}[level]
        relation = params.get("relation", "above")
        passed = latest > previous if relation == "above" else latest < previous
        values = [(bool(ok), float(value), float(threshold), f"close {relation} previous {level}", Direction.NEUTRAL) for ok, value, threshold in zip(passed, latest, previous)]
    elif filter_id == "breakout":
        lookback = int(params.get("lookback", 20))
        direction = params.get("direction", "bullish")
        previous_high = np.max(high_windows[:, -lookback - 1:-1], axis=1)
        previous_low = np.min(low_windows[:, -lookback - 1:-1], axis=1)
        level = previous_high if direction == "bullish" else previous_low
        previous_close = close_windows[:, -2]
        average_volume = np.mean(volume_windows[:, -lookback - 1:-1], axis=1)
        relative_volume = np.divide(volume_windows[:, -1], average_volume, out=np.zeros_like(average_volume), where=average_volume != 0)
        volume_multiple = float(params.get("volume_multiple", 0))
        crossed = previous_close <= level if direction == "bullish" else previous_close >= level
        crossed &= latest > level if direction == "bullish" else latest < level
        passed = crossed & (relative_volume >= volume_multiple if volume_multiple > 0 else True)
        trend = Direction.BULLISH if direction == "bullish" else Direction.BEARISH
        values = [
            (
                bool(ok),
                {"close": float(current), "previous_close": float(previous), "level": float(boundary), "relative_volume": float(volume)},
                {"direction": direction, "volume_multiple": volume_multiple},
                f"{direction} breakout",
                trend,
            )
            for ok, current, previous, boundary, volume in zip(passed, latest, previous_close, level, relative_volume)
        ]
    else:
        return None

    for index, value in enumerate(values, start=length - 1):
        result[index] = value
    return result


def build_atomic_backtest_cache(
    expressions: Iterable[FilterExpression],
    candles_by_timeframe: dict[Timeframe, list[CandlePoint]],
    catalog: FilterCatalog,
) -> AtomicBacktestCache:
    """Precompute deterministic condition values shared by a batch."""
    conditions: dict[tuple[str, int], FilterCondition] = {}
    for expression in expressions:
        lookback: dict[Timeframe, int] = {}
        nodes: list[FilterCondition] = []
        for node in expression.children + expression.veto:
            _collect_conditions(node, nodes)
        for condition in nodes:
            requirement = catalog.requirements_for(
                condition.filter_id,
                condition.params,
                timeframe=condition.timeframe,
            )
            required_candles = requirement.total_candles
            if condition.filter_id == "data_completeness":
                required_candles = max(
                    required_candles,
                    int(condition.params.get("minimum_candles", 30)),
                )
            lookback[condition.timeframe] = max(
                lookback.get(condition.timeframe, 0),
                required_candles,
            )
        for condition in nodes:
            if condition.filter_id in FilterEvaluator._CACHEABLE_ATOMIC_FILTERS:
                key = FilterEvaluator.condition_cache_key(condition)
                conditions.setdefault((key, lookback[condition.timeframe]), condition)

    cache = AtomicBacktestCache()
    for (key, context_length), condition in conditions.items():
        points = candles_by_timeframe.get(condition.timeframe, [])
        values = _condition_result_series(condition, points, catalog, context_length)
        if values is not None:
            cache.add(key, context_length, [point.timestamp for point in points], values)
    return cache
