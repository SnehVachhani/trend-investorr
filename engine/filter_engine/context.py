"""Construction and normalization of point-in-time evaluation contexts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from data.models.market_data import Candle

from .models import CandlePoint, EvaluationContext, EvaluationMode, Timeframe


def _to_point(candle: Candle | CandlePoint | dict[str, Any]) -> CandlePoint:
    if isinstance(candle, CandlePoint):
        return candle
    if isinstance(candle, Candle):
        return CandlePoint(
            symbol=candle.symbol,
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            interval=candle.interval.value,
            delivery_pct=candle.delivery_pct,
            vwap=candle.vwap,
        )
    return CandlePoint.model_validate(candle)


def build_evaluation_context(
    *,
    symbol: str,
    as_of: datetime,
    mode: EvaluationMode,
    candles: dict[Timeframe, Iterable[Candle | CandlePoint | dict[str, Any]]] | None = None,
    computed_features: dict[str, Any] | None = None,
    stock_metadata: dict[str, Any] | None = None,
    live_snapshot: dict[str, Any] | None = None,
    index_context: dict[str, Any] | None = None,
    event_flags: dict[str, Any] | None = None,
    data_quality: dict[str, Any] | None = None,
    trendlines: list[dict[str, Any]] | None = None,
) -> EvaluationContext:
    normalized: dict[Timeframe, list[CandlePoint]] = {}
    for timeframe, series in (candles or {}).items():
        points = [_to_point(candle) for candle in series]
        points.sort(key=lambda item: item.timestamp)
        if mode is EvaluationMode.BACKTEST:
            points = [point for point in points if point.timestamp <= as_of]
        normalized[timeframe] = points
    return EvaluationContext(
        symbol=symbol,
        as_of=as_of,
        mode=mode,
        candles=normalized,
        computed_features=computed_features or {},
        stock_metadata=stock_metadata or {},
        live_snapshot=live_snapshot or {},
        index_context=index_context or {},
        event_flags=event_flags or {},
        data_quality=data_quality or {},
        trendlines=trendlines or [],
    )


def build_fast_backtest_context(
    *,
    symbol: str,
    as_of: datetime,
    candles: dict[Timeframe, list[CandlePoint]],
    computed_features: dict[str, Any] | None = None,
    stock_metadata: dict[str, Any] | None = None,
    live_snapshot: dict[str, Any] | None = None,
    index_context: dict[str, Any] | None = None,
    event_flags: dict[str, Any] | None = None,
    data_quality: dict[str, Any] | None = None,
    trendlines: list[dict[str, Any]] | None = None,
) -> EvaluationContext:
    """Build an already-normalized backtest context without Pydantic scans.

    ``ProfileBacktester`` constructs sorted, point-in-time candle windows before
    calling this helper.  Re-running model validation and scanning every candle
    for future timestamps at every five-minute bar is both redundant and a
    measurable part of the all-combinations latency.
    """
    return EvaluationContext.model_construct(
        symbol=symbol,
        as_of=as_of,
        mode=EvaluationMode.BACKTEST,
        candles=candles,
        computed_features=computed_features or {},
        stock_metadata=stock_metadata or {},
        live_snapshot=live_snapshot or {},
        index_context=index_context or {},
        event_flags=event_flags or {},
        data_quality=data_quality or {},
        trendlines=trendlines or [],
    )
