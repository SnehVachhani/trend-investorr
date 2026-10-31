"""Enhanced, daily swing-momentum ranking.

This module deliberately contains pure feature and scoring functions.  The
existing momentum and live scanners remain unchanged; the API layer is
responsible for loading daily candles and passing compact rows here.

The score is intentionally explainable:

* base momentum: multi-horizon absolute returns;
* market relative strength: stock return less the Nifty return;
* risk-adjusted momentum: return relative to realised volatility and ATR;
* breakout quality: proximity/breakout above prior ranges, participation and
  a penalty for an already overextended entry;
Sector context is an optional input with a zero default weight.  This keeps
the future extension point explicit without pretending sector history is
available today.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite, log, sqrt
from statistics import pstdev
from typing import Any, Iterable


ENHANCED_WEIGHTS = {
    "base_momentum": 0.45,
    "market_relative_strength": 0.25,
    "risk_adjusted_momentum": 0.20,
    "breakout_quality": 0.10,
    # Reserved for a future point-in-time sector feature.  It is deliberately
    # zero today so sector data cannot accidentally influence the ranking.
    "sector_context": 0.0,
}


@dataclass(frozen=True)
class BenchmarkSummary:
    """Compact point-in-time summary for one market benchmark."""

    symbol: str
    available: bool
    observations: int
    latest_timestamp: Any = None
    latest_close: float | None = None
    return_20d: float | None = None
    return_60d: float | None = None
    return_120d: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    realized_volatility_pct: float | None = None
    trend: str = "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "available": self.available,
            "observations": self.observations,
            "latest_timestamp": _json_value(self.latest_timestamp),
            "latest_close": _rounded(self.latest_close),
            "return_20d": _rounded(self.return_20d),
            "return_60d": _rounded(self.return_60d),
            "return_120d": _rounded(self.return_120d),
            "sma_50": _rounded(self.sma_50),
            "sma_200": _rounded(self.sma_200),
            "realized_volatility_pct": _rounded(self.realized_volatility_pct),
            "trend": self.trend,
        }


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _rounded(value: Any, digits: int = 4) -> float | None:
    parsed = _number(value)
    return round(parsed, digits) if parsed is not None else None


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _return(values: list[float], period: int) -> float | None:
    if len(values) <= period or values[-1] <= 0 or values[-period - 1] <= 0:
        return None
    return (values[-1] / values[-period - 1] - 1.0) * 100.0


def _true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        high = _number(candle.get("high"))
        low = _number(candle.get("low"))
        close = _number(candle.get("close"))
        if high is None or low is None or close is None:
            previous_close = close
            continue
        parts = [high - low]
        if previous_close is not None:
            parts.extend((abs(high - previous_close), abs(low - previous_close)))
        ranges.append(max(parts))
        previous_close = close
    return ranges


def _realized_volatility_pct(closes: list[float], period: int = 20) -> float | None:
    if len(closes) <= period:
        return None
    window = closes[-period - 1:]
    returns = [log(window[index] / window[index - 1]) for index in range(1, len(window)) if window[index - 1] > 0 and window[index] > 0]
    if len(returns) < max(5, period // 2):
        return None
    return pstdev(returns) * sqrt(252.0) * 100.0


def summarize_benchmark(symbol: str, candles: Iterable[dict[str, Any]]) -> BenchmarkSummary:
    """Create a benchmark summary from ascending daily candles."""
    ordered = list(candles)
    closes = [_number(row.get("close")) for row in ordered]
    closes = [value for value in closes if value is not None and value > 0]
    if not closes:
        return BenchmarkSummary(symbol=symbol, available=False, observations=0)

    latest_timestamp = ordered[-1].get("timestamp") if ordered else None
    latest_close = closes[-1]
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)
    return_60d = _return(closes, 60)
    bullish = (
        sma_50 is not None
        and sma_200 is not None
        and latest_close > sma_50 > sma_200
        and (return_60d is None or return_60d > 0)
    )
    bearish = (
        sma_50 is not None
        and sma_200 is not None
        and latest_close < sma_50 < sma_200
        and (return_60d is None or return_60d < 0)
    )
    trend = "bullish" if bullish else "bearish" if bearish else "neutral"
    return BenchmarkSummary(
        symbol=symbol,
        available=len(closes) >= 60,
        observations=len(closes),
        latest_timestamp=latest_timestamp,
        latest_close=latest_close,
        return_20d=_return(closes, 20),
        return_60d=return_60d,
        return_120d=_return(closes, 120),
        sma_50=sma_50,
        sma_200=sma_200,
        realized_volatility_pct=_realized_volatility_pct(closes),
        trend=trend,
    )


def build_market_context(
    nifty_candles: Iterable[dict[str, Any]],
    sensex_candles: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a market context using Nifty as primary and Sensex as confirmation."""
    nifty = summarize_benchmark("NIFTY", nifty_candles)
    sensex = summarize_benchmark("SENSEX", sensex_candles)
    available = nifty.available or sensex.available
    available_trends = [item.trend for item in (nifty, sensex) if item.available]

    if not available_trends:
        regime = "unavailable"
        regime_score = None
    elif all(item == "bullish" for item in available_trends):
        regime = "bullish"
        regime_score = 100.0
    elif all(item == "bearish" for item in available_trends):
        regime = "bearish"
        regime_score = 0.0
    elif "bullish" in available_trends and "bearish" in available_trends:
        regime = "mixed"
        regime_score = 50.0
    else:
        regime = "neutral"
        regime_score = 50.0

    return {
        "available": available,
        "regime": regime,
        "regime_score": _rounded(regime_score),
        "primary_benchmark": "NIFTY" if nifty.available else "SENSEX" if sensex.available else None,
        "nifty": nifty.as_dict(),
        "sensex": sensex.as_dict(),
        "warnings": [
            f"{label} daily benchmark history is unavailable or too short"
            for label, summary in (("Nifty", nifty), ("Sensex", sensex))
            if not summary.available
        ],
    }


def _percentile(value: float | None, values: list[float], higher_is_better: bool = True) -> float | None:
    if value is None or not values:
        return None
    clean = sorted(item for item in values if isfinite(item))
    if not clean:
        return None
    rank = sum(item <= value for item in clean) / len(clean) * 100.0
    return rank if higher_is_better else 100.0 - rank


def _component_percentile(rows: list[dict[str, Any]], key: str) -> dict[int, float | None]:
    values = [_number(row.get(key)) for row in rows]
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return {index: None for index in range(len(values))}
    return {
        index: (bisect_right(clean, value) / len(clean) * 100.0 if value is not None else None)
        for index, value in enumerate(values)
    }


def _score_average(parts: list[tuple[float, float | None]]) -> float | None:
    available = [(weight, score) for weight, score in parts if score is not None]
    if not available:
        return None
    total_weight = sum(weight for weight, _ in available)
    return sum(weight * score for weight, score in available) / total_weight


def enrich_candidates(
    rows: Iterable[dict[str, Any]],
    market_context: dict[str, Any],
    *,
    min_history: int = 260,
) -> list[dict[str, Any]]:
    """Add enhanced features and explainable scores to compact stock rows.

    The optional sector hook is intentionally represented in every result but
    has no weight until a point-in-time sector feature is supplied later.
    """
    candidates: list[dict[str, Any]] = []
    nifty = market_context.get("nifty") or {}
    nifty_returns = {
        20: _number(nifty.get("return_20d")),
        60: _number(nifty.get("return_60d")),
        120: _number(nifty.get("return_120d")),
    }
    regime_score = _number(market_context.get("regime_score"))

    for source in rows:
        row = dict(source)
        return_20 = _number(row.get("return_20d"))
        return_60 = _number(row.get("return_60d"))
        return_120 = _number(row.get("return_120d"))
        return_252 = _number(row.get("return_252d"))
        realized_volatility = {
            horizon: _number(row.get(f"realized_volatility_{horizon}d"))
            for horizon in (20, 60, 120, 252)
        }
        # Keep compatibility with compact rows created before the multi-horizon
        # fields were introduced.  The API now supplies all four horizons.
        realized_volatility[20] = realized_volatility[20] or _number(row.get("realized_volatility_pct"))
        atr_pct = {
            horizon: _number(row.get(f"atr_pct_{horizon}d"))
            for horizon in (20, 60, 120, 252)
        }
        atr_abs_by_horizon = {
            horizon: _number(row.get(f"atr_abs_{horizon}d"))
            for horizon in (20, 60, 120, 252)
        }
        prior_high_20 = _number(row.get("prior_high_20"))
        prior_high_55 = _number(row.get("prior_high_55"))
        close = _number(row.get("latest_close"))
        atr_abs = atr_abs_by_horizon[20]
        volume_ratios = {
            horizon: _number(row.get(f"volume_ratio_{horizon}d"))
            for horizon in (20, 60, 120, 252)
        }
        volume_ratios[20] = volume_ratios[20] or _number(row.get("volume_ratio"))
        average_turnover = {
            horizon: _number(row.get(f"avg_turnover_{horizon}d_cr"))
            for horizon in (20, 60, 120, 252)
        }
        close_position = _number(row.get("close_position")) or 0.0

        relative_values = {
            20: return_20 - nifty_returns[20] if return_20 is not None and nifty_returns[20] is not None else None,
            60: return_60 - nifty_returns[60] if return_60 is not None and nifty_returns[60] is not None else None,
            120: return_120 - nifty_returns[120] if return_120 is not None and nifty_returns[120] is not None else None,
        }
        returns = {20: return_20, 60: return_60, 120: return_120, 252: return_252}
        risk_adjusted = {
            horizon: (
                returns[horizon]
                / max(realized_volatility[horizon] or atr_pct[horizon] or 0.0, 0.25)
                if returns[horizon] is not None
                else None
            )
            for horizon in (20, 60, 120, 252)
        }
        breakout_margin = ((close - prior_high_20) / atr_abs) if close is not None and prior_high_20 is not None and atr_abs and atr_abs > 0 else None
        breakout_20 = bool(close is not None and prior_high_20 is not None and close > prior_high_20)
        breakout_55 = bool(close is not None and prior_high_55 is not None and close > prior_high_55)
        proximity_20 = _clamp((close / prior_high_20 - 0.95) / 0.10 * 100.0) if close and prior_high_20 else None
        proximity_55 = _clamp((close / prior_high_55 - 0.92) / 0.12 * 100.0) if close and prior_high_55 else None
        volume_ratio_score = _score_average(
            [
                (0.40, _clamp((volume_ratios[20] - 1.0) / 2.0 * 100.0) if volume_ratios[20] is not None else None),
                (0.30, _clamp((volume_ratios[60] - 1.0) / 2.0 * 100.0) if volume_ratios[60] is not None else None),
                (0.20, _clamp((volume_ratios[120] - 1.0) / 2.0 * 100.0) if volume_ratios[120] is not None else None),
                (0.10, _clamp((volume_ratios[252] - 1.0) / 2.0 * 100.0) if volume_ratios[252] is not None else None),
            ]
        )
        turnover_score = _score_average(
            [
                (0.40, _clamp(average_turnover[20] / 10.0 * 100.0) if average_turnover[20] is not None else None),
                (0.30, _clamp(average_turnover[60] / 10.0 * 100.0) if average_turnover[60] is not None else None),
                (0.20, _clamp(average_turnover[120] / 10.0 * 100.0) if average_turnover[120] is not None else None),
                (0.10, _clamp(average_turnover[252] / 10.0 * 100.0) if average_turnover[252] is not None else None),
            ]
        )
        participation_score = _score_average(
            [(0.65, volume_ratio_score), (0.35, turnover_score)]
        )
        extension_atr = ((close - _number(row.get("sma_20"))) / atr_abs) if close and _number(row.get("sma_20")) and atr_abs and atr_abs > 0 else None
        extension_penalty = _clamp((extension_atr - 2.5) / 2.5 * 100.0) if extension_atr is not None else 0.0
        breakout_quality_raw = _score_average(
            [
                (0.35, proximity_20),
                (0.25, proximity_55),
                (0.25, participation_score),
                (0.15, close_position * 100.0),
            ]
        )
        if breakout_quality_raw is not None:
            breakout_quality_raw = _clamp(breakout_quality_raw - extension_penalty * 0.35)

        row.update(
            {
                "market_relative_20d": relative_values[20],
                "market_relative_60d": relative_values[60],
                "market_relative_120d": relative_values[120],
                "risk_adjusted_20d": risk_adjusted[20],
                "risk_adjusted_60d": risk_adjusted[60],
                "risk_adjusted_120d": risk_adjusted[120],
                "risk_adjusted_252d": risk_adjusted[252],
                "volume_participation_score": participation_score,
                "volume_ratio_score": volume_ratio_score,
                "turnover_score": turnover_score,
                "volume_ratio_20d": volume_ratios[20],
                "volume_ratio_60d": volume_ratios[60],
                "volume_ratio_120d": volume_ratios[120],
                "volume_ratio_252d": volume_ratios[252],
                "breakout_20": breakout_20,
                "breakout_55": breakout_55,
                "breakout_margin_atr": breakout_margin,
                "extension_atr": extension_atr,
                "breakout_quality_raw": breakout_quality_raw,
                "sector_score": None,
                "sector_context_available": False,
                "history_quality": _clamp((_number(row.get("observations")) or 0) / max(min_history, 1) * 100.0),
            }
        )
        candidates.append(row)

    base20 = _component_percentile(candidates, "return_20d")
    base60 = _component_percentile(candidates, "return_60d")
    base120 = _component_percentile(candidates, "return_120d")
    base252 = _component_percentile(candidates, "return_252d")
    relative20 = _component_percentile(candidates, "market_relative_20d")
    relative60 = _component_percentile(candidates, "market_relative_60d")
    relative120 = _component_percentile(candidates, "market_relative_120d")
    risk20 = _component_percentile(candidates, "risk_adjusted_20d")
    risk60 = _component_percentile(candidates, "risk_adjusted_60d")
    risk120 = _component_percentile(candidates, "risk_adjusted_120d")
    risk252 = _component_percentile(candidates, "risk_adjusted_252d")
    breakout = _component_percentile(candidates, "breakout_quality_raw")

    scored: list[dict[str, Any]] = []
    for index, row in enumerate(candidates):
        base_score = _score_average(
            [(0.25, base20[index]), (0.30, base60[index]), (0.25, base120[index]), (0.20, base252[index])]
        )
        relative_score = _score_average(
            [(0.35, relative20[index]), (0.40, relative60[index]), (0.25, relative120[index])]
        )
        risk_score = _score_average(
            [
                (0.20, risk20[index]),
                (0.30, risk60[index]),
                (0.25, risk120[index]),
                (0.25, risk252[index]),
            ]
        )
        breakout_score = breakout[index]
        regime_component = regime_score if market_context.get("available") else None
        final_score = _score_average(
            [
                (ENHANCED_WEIGHTS["base_momentum"], base_score),
                (ENHANCED_WEIGHTS["market_relative_strength"], relative_score),
                (ENHANCED_WEIGHTS["risk_adjusted_momentum"], risk_score),
                (ENHANCED_WEIGHTS["breakout_quality"], breakout_score),
                (ENHANCED_WEIGHTS["sector_context"], _number(row.get("sector_score"))),
            ]
        ) or 0.0
        history = _number(row.get("observations")) or 0.0
        row.update(
            {
                "eligible": history >= min_history,
                "enhanced_score": round(final_score, 2),
                "base_momentum_score": _rounded(base_score, 2),
                "market_relative_strength_score": _rounded(relative_score, 2),
                "risk_adjusted_momentum_score": _rounded(risk_score, 2),
                "breakout_quality_score": _rounded(breakout_score, 2),
                "market_regime_score": _rounded(regime_component, 2),
                "score_weights": dict(ENHANCED_WEIGHTS),
                "category": (
                    "Breakout leader" if row.get("breakout_55") and final_score >= 70
                    else "Market-relative leader" if relative_score is not None and relative_score >= 75
                    else "Risk-adjusted trend" if risk_score is not None and risk_score >= 75
                    else "Enhanced momentum"
                ),
            }
        )
        scored.append(row)
    return scored


__all__ = [
    "ENHANCED_WEIGHTS",
    "BenchmarkSummary",
    "build_market_context",
    "enrich_candidates",
    "summarize_benchmark",
]
