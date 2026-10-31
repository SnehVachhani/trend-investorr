"""Pure atomic filters and recursive expression evaluation."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Callable

import numpy as np

from engine.indicator_engine import IndicatorEngine
from engine.swing_volume_profile import build_swing_volume_profile_states

from .catalog import FilterCatalog, build_default_catalog
from .models import (
    Direction,
    EvaluationContext,
    EvaluationMode,
    FilterCondition,
    FilterExpression,
    FilterGroup,
    FilterResult,
    FilterStatus,
    GroupOperator,
    RuleNode,
    Timeframe,
)


class FilterEvaluator:
    """Evaluate whitelisted filter expressions without side effects or order placement."""

    _CACHEABLE_ATOMIC_FILTERS = frozenset(
        {
            "data_completeness",
            "price_range",
            "average_volume",
            "average_traded_value",
            "price_above_sma",
            "ema_relation",
            "rsi_range",
            "macd_direction",
            "atr_percent",
            "adx_threshold",
            "relative_volume",
            "previous_level",
            "breakout",
            "bullish_swing_poc_pullback",
        }
    )

    def __init__(self, catalog: FilterCatalog | None = None, atomic_cache=None):
        self.catalog = catalog or build_default_catalog()
        # Prepared expressions avoid repeating catalog and parameter validation
        # for every candle. The cache is intentionally tied to this evaluator
        # instance, which is scoped to one request or one shared batch.
        self._prepared_expression_ids: set[int] = set()
        self._prepared_conditions: dict[int, tuple[Any, Any, str]] = {}
        self.atomic_cache = atomic_cache

    @staticmethod
    def condition_cache_key(condition: FilterCondition) -> str:
        """Return a stable key for a deterministic condition calculation."""
        return json.dumps(
            [condition.filter_id, condition.timeframe.value, condition.params],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def prepare(self, expression: FilterExpression, mode: EvaluationMode) -> None:
        """Validate and index an expression once before a candle loop."""
        expression_id = id(expression)
        if expression_id in self._prepared_expression_ids:
            return
        self.catalog.validate_expression(expression, mode)

        def visit(node: RuleNode) -> None:
            if isinstance(node, FilterGroup):
                for child in node.children:
                    visit(child)
                return
            self._prepared_conditions[id(node)] = (
                self.catalog.get(node.filter_id),
                self.catalog.requirements_for(
                    node.filter_id,
                    node.params,
                    timeframe=node.timeframe,
                ),
                self.condition_cache_key(node),
            )

        for node in expression.children + expression.veto:
            visit(node)
        self._prepared_expression_ids.add(expression_id)

    def evaluate(self, expression: FilterExpression, context: EvaluationContext) -> FilterResult:
        self.prepare(expression, context.mode)
        children = [self._evaluate_node(node, context) for node in expression.children]
        veto_results = [self._evaluate_node(node, context) for node in expression.veto]
        root = self._combine(expression.operator, children, context, expression)
        root.node_type = "expression"
        root.operator = expression.operator
        root.veto_results = veto_results
        root.children = children

        if any(result.status is not FilterStatus.PASS for result in veto_results):
            root.status = next(
                (result.status for result in veto_results if result.status is not FilterStatus.PASS),
                FilterStatus.FAIL,
            )
            root.passed = False
            root.reason = "one or more mandatory veto gates did not pass"
        elif root.status in {FilterStatus.ERROR, FilterStatus.UNAVAILABLE}:
            # Preserve data/context availability failures.  They are not a
            # normal strategy rejection and must remain visible to callers.
            pass
        elif root.score < expression.minimum_score:
            root.status = FilterStatus.FAIL
            root.passed = False
            root.reason = f"score {root.score:.3f} is below minimum {expression.minimum_score:.3f}"
        elif self._count_insufficient(root) > expression.maximum_insufficient_data:
            insufficient = self._count_insufficient(root)
            root.status = FilterStatus.INSUFFICIENT_DATA
            root.passed = False
            root.reason = (
                f"{insufficient} conditions have insufficient data; "
                f"maximum allowed is {expression.maximum_insufficient_data}"
            )
        return root

    @staticmethod
    def _count_insufficient(result: FilterResult) -> int:
        """Count atomic insufficient-data results in a composed expression."""
        count = int(
            result.node_type == "condition"
            and result.status is FilterStatus.INSUFFICIENT_DATA
        )
        return count + sum(
            FilterEvaluator._count_insufficient(child)
            for child in result.children
        ) + sum(
            FilterEvaluator._count_insufficient(veto)
            for veto in result.veto_results
        )

    def _evaluate_node(self, node: RuleNode, context: EvaluationContext) -> FilterResult:
        if isinstance(node, FilterCondition):
            return self._evaluate_condition(node, context)
        children = [self._evaluate_node(child, context) for child in node.children]
        return self._combine(node.operator, children, context, node)

    def _evaluate_condition(self, condition: FilterCondition, context: EvaluationContext) -> FilterResult:
        prepared = self._prepared_conditions.get(id(condition))
        if prepared is None:
            self.prepare(
                FilterExpression(children=[condition]),
                context.mode,
            )
            prepared = self._prepared_conditions[id(condition)]
        definition, requirement, cache_key = prepared
        candles = context.candles.get(condition.timeframe, [])
        needs_live = definition.requires_live and context.mode is EvaluationMode.SIGNAL
        if needs_live and not context.live_snapshot:
            return self._result(
                condition,
                context,
                FilterStatus.UNAVAILABLE,
                False,
                reason="live quote/order-book data is unavailable",
                required_history=requirement,
            )
        missing_context_reason = None
        if condition.filter_id == "relative_strength" and not context.index_context:
            missing_context_reason = "index or sector context is unavailable"
        elif condition.filter_id == "trendline_break" and not context.trendlines:
            missing_context_reason = "symbol trendline context is unavailable"
        elif condition.filter_id == "event_risk" and "event_risk" not in context.event_flags:
            missing_context_reason = "historical event-risk context is unavailable"
        elif (
            condition.filter_id == "circuit_status"
            and "status" not in context.live_snapshot
            and "status" not in context.stock_metadata
        ):
            missing_context_reason = "historical circuit-status context is unavailable"
        if missing_context_reason:
            return self._result(
                condition,
                context,
                FilterStatus.UNAVAILABLE,
                False,
                reason=missing_context_reason,
                required_history=requirement,
            )
        required_count = requirement.total_candles
        if condition.filter_id == "data_completeness":
            required_count = max(required_count, int(condition.params.get("minimum_candles", 30)))
        if len(candles) < required_count and condition.filter_id not in {"event_risk", "circuit_status"}:
            return self._result(
                condition,
                context,
                FilterStatus.INSUFFICIENT_DATA,
                False,
                reason=f"requires {required_count} candles, received {len(candles)}",
                required_history=requirement,
            )
        try:
            cached = None
            if self.atomic_cache is not None and condition.filter_id in self._CACHEABLE_ATOMIC_FILTERS:
                latest_timestamp = candles[-1].timestamp if candles else None
                cached = self.atomic_cache.get(cache_key, latest_timestamp, len(candles))
            if cached is None:
                cached = self._run_atomic(condition, context, candles)
                if self.atomic_cache is not None and condition.filter_id in self._CACHEABLE_ATOMIC_FILTERS:
                    latest_timestamp = candles[-1].timestamp if candles else None
                    self.atomic_cache.set(cache_key, latest_timestamp, len(candles), cached)
            passed, actual, threshold, reason, direction = cached
            return self._result(
                condition,
                context,
                FilterStatus.PASS if passed else FilterStatus.FAIL,
                passed,
                actual_value=actual,
                threshold=threshold,
                reason=reason,
                direction=direction,
                required_history=requirement,
                data_timestamp=candles[-1].timestamp if candles else None,
            )
        except Exception as exc:
            return self._result(
                condition,
                context,
                FilterStatus.ERROR,
                False,
                reason=f"filter evaluation error: {exc}",
                required_history=requirement,
            )

    def _run_atomic(self, condition: FilterCondition, context: EvaluationContext, candles):
        filter_id = condition.filter_id
        params = condition.params
        closes = np.asarray([c.close for c in candles], dtype=float)
        highs = np.asarray([c.high for c in candles], dtype=float)
        lows = np.asarray([c.low for c in candles], dtype=float)
        volumes = np.asarray([c.volume for c in candles], dtype=float)
        latest = float(closes[-1]) if len(closes) else None

        if filter_id == "data_completeness":
            minimum = int(params.get("minimum_candles", 30))
            return len(candles) >= minimum, len(candles), minimum, f"received {len(candles)} candles", Direction.NEUTRAL
        if filter_id == "price_range":
            minimum = params.get("min_price")
            maximum = params.get("max_price")
            passed = (minimum is None or latest >= minimum) and (maximum is None or latest <= maximum)
            return passed, latest, {"min": minimum, "max": maximum}, f"close={latest:.2f}", Direction.NEUTRAL
        if filter_id == "average_volume":
            period = int(params.get("period", 30))
            actual = float(np.mean(volumes[-period:]))
            threshold = float(params.get("minimum", 0))
            return actual >= threshold, actual, threshold, f"average volume={actual:.0f}", Direction.NEUTRAL
        if filter_id == "average_traded_value":
            period = int(params.get("period", 30))
            actual = float(np.mean(closes[-period:] * volumes[-period:]))
            threshold = float(params.get("minimum", 0))
            return actual >= threshold, actual, threshold, f"average traded value={actual:.2f}", Direction.NEUTRAL
        if filter_id == "price_above_sma":
            period = int(params.get("period", 200))
            average = float(IndicatorEngine.compute_sma(closes, period)[-1])
            relation = params.get("relation", "above")
            passed = latest > average if relation == "above" else latest < average
            direction = Direction.BULLISH if relation == "above" else Direction.BEARISH
            return passed, latest, average, f"close {relation} SMA({period})", direction
        if filter_id == "ema_relation":
            fast_period = int(params.get("fast_period", 20))
            slow_period = int(params.get("slow_period", 50))
            fast = float(IndicatorEngine.compute_ema(closes, fast_period)[-1])
            slow = float(IndicatorEngine.compute_ema(closes, slow_period)[-1])
            relation = params.get("relation", "above")
            passed = fast > slow if relation == "above" else fast < slow
            direction = Direction.BULLISH if relation == "above" else Direction.BEARISH
            return passed, {"fast": fast, "slow": slow}, relation, f"EMA({fast_period}) {relation} EMA({slow_period})", direction
        if filter_id == "rsi_range":
            period = int(params.get("period", 14))
            actual = float(IndicatorEngine.compute_rsi(closes, period)[-1])
            minimum = float(params.get("minimum", 40))
            maximum = float(params.get("maximum", 70))
            return minimum <= actual <= maximum, actual, {"min": minimum, "max": maximum}, f"RSI={actual:.2f}", Direction.NEUTRAL
        if filter_id == "macd_direction":
            line, signal, histogram = IndicatorEngine.compute_macd(closes)
            actual = {"line": float(line[-1]), "signal": float(signal[-1]), "histogram": float(histogram[-1])}
            direction = params.get("direction", "bullish")
            bullish = actual["line"] > actual["signal"]
            bearish = actual["line"] < actual["signal"]
            passed = bullish if direction == "bullish" else bearish
            actual_direction = Direction.BULLISH if bullish else Direction.BEARISH if bearish else Direction.NEUTRAL
            return passed, actual, direction, f"MACD direction={direction}", actual_direction
        if filter_id == "atr_percent":
            period = int(params.get("period", 14))
            atr = float(IndicatorEngine.compute_atr(highs, lows, closes, period)[-1])
            actual = (atr / latest * 100) if latest else 0.0
            minimum = float(params.get("minimum", 0))
            maximum = float(params.get("maximum", 10))
            return minimum <= actual <= maximum, actual, {"min": minimum, "max": maximum}, f"ATR%={actual:.2f}", Direction.NEUTRAL
        if filter_id == "adx_threshold":
            period = int(params.get("period", 14))
            actual = float(IndicatorEngine.compute_adx(highs, lows, closes, period)[-1])
            threshold = float(params.get("minimum", 20))
            return actual >= threshold, actual, threshold, f"ADX={actual:.2f}", Direction.NEUTRAL
        if filter_id == "relative_volume":
            period = int(params.get("period", 20))
            average = float(np.mean(volumes[-period - 1:-1]))
            actual = (float(volumes[-1]) / average) if average else 0.0
            threshold = float(params.get("multiple", 1.5))
            return actual >= threshold, actual, threshold, f"relative volume={actual:.2f}x", Direction.NEUTRAL
        if filter_id == "price_above_vwap":
            # Historical intraday VWAP resets at the start of each session.
            # Using the entire lookback window would turn it into a multi-day
            # average and produce false signals.
            vwap = None if context.mode is EvaluationMode.BACKTEST else context.live_snapshot.get("vwap")
            vwap_candles = candles
            if condition.timeframe in {
                Timeframe.ONE_MINUTE,
                Timeframe.FIVE_MINUTE,
                Timeframe.FIFTEEN_MINUTE,
                Timeframe.ONE_HOUR,
            } and candles:
                session_date = candles[-1].timestamp.date()
                vwap_candles = [candle for candle in candles if candle.timestamp.date() == session_date]
            if vwap is None:
                session_highs = np.asarray([c.high for c in vwap_candles], dtype=float)
                session_lows = np.asarray([c.low for c in vwap_candles], dtype=float)
                session_closes = np.asarray([c.close for c in vwap_candles], dtype=float)
                session_volumes = np.asarray([c.volume for c in vwap_candles], dtype=float)
                typical = (session_highs + session_lows + session_closes) / 3
                vwap = float(np.sum(typical * session_volumes) / np.sum(session_volumes)) if np.sum(session_volumes) else None
            relation = params.get("relation", "above")
            passed = latest > float(vwap) if relation == "above" else latest < float(vwap)
            return passed, latest, float(vwap), f"close {relation} VWAP", Direction.BULLISH if relation == "above" else Direction.BEARISH
        if filter_id == "previous_level":
            level = params.get("level", "high")
            previous = candles[-2]
            threshold = float(getattr(previous, level))
            relation = params.get("relation", "above")
            passed = latest > threshold if relation == "above" else latest < threshold
            return passed, latest, threshold, f"close {relation} previous {level}", Direction.NEUTRAL
        if filter_id == "support_resistance":
            support, resistance = IndicatorEngine.compute_support_resistance(highs, lows, closes, window=5)
            level = params.get("level", "support")
            threshold = float(support if level == "support" else resistance)
            distance = ((latest - threshold) / threshold * 100) if threshold else 0.0
            minimum = float(params.get("minimum_distance_pct", 0))
            passed = distance >= minimum if level == "support" else distance <= -minimum
            return passed, distance, minimum, f"distance to {level}={distance:.2f}%", Direction.NEUTRAL
        if filter_id == "relative_strength":
            excess = context.index_context.get("excess_return_pct")
            if excess is None:
                stock_return = context.index_context.get("stock_return_pct")
                benchmark_return = context.index_context.get("benchmark_return_pct")
                if stock_return is not None and benchmark_return is not None:
                    excess = float(stock_return) - float(benchmark_return)
            if excess is None:
                raise ValueError("index or sector relative-strength context is unavailable")
            threshold = float(params.get("minimum_excess_return_pct", 0))
            return float(excess) >= threshold, float(excess), threshold, f"excess return={float(excess):.2f}%", Direction.BULLISH
        if filter_id == "breakout":
            lookback = int(params.get("lookback", 20))
            direction = params.get("direction", "bullish")
            previous_high = float(np.max(highs[-lookback - 1:-1]))
            previous_low = float(np.min(lows[-lookback - 1:-1]))
            level = previous_high if direction == "bullish" else previous_low
            volume_multiple = float(params.get("volume_multiple", 0))
            relative_volume = float(volumes[-1] / np.mean(volumes[-lookback - 1:-1])) if np.mean(volumes[-lookback - 1:-1]) else 0.0
            previous_close = float(closes[-2])
            # A breakout is a crossing event, not every later candle that
            # remains beyond the stale range boundary.
            passed = (
                previous_close <= level and latest > level
                if direction == "bullish"
                else previous_close >= level and latest < level
            )
            if volume_multiple > 0:
                passed = passed and relative_volume >= volume_multiple
            return passed, {"close": latest, "previous_close": previous_close, "level": level, "relative_volume": relative_volume}, {"direction": direction, "volume_multiple": volume_multiple}, f"{direction} breakout", Direction.BULLISH if direction == "bullish" else Direction.BEARISH
        if filter_id == "bullish_swing_poc_pullback":
            state = build_swing_volume_profile_states(
                candles,
                lookback=int(params.get("lookback", 250)),
                pivot_window=int(params.get("pivot_window", 3)),
                value_area_percent=float(params.get("value_area_percent", 70)),
                zone_buffer_pct=float(params.get("zone_buffer_pct", 0.25)),
            )[-1]
            actual = state.as_dict()
            return (
                state.signal,
                actual,
                {"value_area_percent": float(params.get("value_area_percent", 70))},
                "bullish swing pullback rejected from the volume-profile value area"
                if state.signal
                else "no confirmed bullish swing pullback rejection at the volume-profile value area",
                Direction.BULLISH if state.bullish_trend else Direction.NEUTRAL,
            )
        if filter_id == "trendline_break":
            trendline_id = int(params["trendline_id"])
            trendline = next(
                (item for item in context.trendlines if int(item.get("id", -1)) == trendline_id),
                None,
            )
            if not trendline:
                raise ValueError(f"trendline {trendline_id} is unavailable for {context.symbol}")
            points = trendline.get("points_json", trendline.get("points", []))
            if len(points) < 2 or len(candles) < 2:
                raise ValueError("trendline break requires two points and two candles")

            def coordinate(point: dict[str, Any]) -> float:
                value = point.get("time", point.get("timestamp"))
                if isinstance(value, datetime):
                    return value.timestamp()
                if isinstance(value, str):
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                return float(value)

            first, second = points[0], points[1]
            x1, x2 = coordinate(first), coordinate(second)
            if x1 == x2:
                raise ValueError("trendline points must have different timestamps")
            slope = (float(second["price"]) - float(first["price"])) / (x2 - x1)

            def line_at(timestamp: datetime) -> float:
                return float(first["price"]) + slope * (timestamp.timestamp() - x1)

            previous = candles[-2]
            previous_line = line_at(previous.timestamp)
            current_line = line_at(candles[-1].timestamp)
            buffer_pct = float(params.get("buffer_pct", 0)) / 100
            direction = params.get("direction", "breaks_above")
            if direction == "breaks_above":
                previous_limit = previous_line * (1 + buffer_pct)
                current_limit = current_line * (1 + buffer_pct)
                passed = previous.close <= previous_limit and latest > current_limit
                trend = Direction.BULLISH
            else:
                previous_limit = previous_line * (1 - buffer_pct)
                current_limit = current_line * (1 - buffer_pct)
                passed = previous.close >= previous_limit and latest < current_limit
                trend = Direction.BEARISH
            return (
                passed,
                {"close": latest, "line": current_line, "previous_close": previous.close, "previous_line": previous_line},
                current_line,
                f"price {direction} trendline {trendline_id}",
                trend,
            )
        if filter_id == "event_risk":
            blocked = bool(context.event_flags.get("event_risk", False))
            return not blocked, blocked, False, "event risk is active" if blocked else "no configured event risk", Direction.NEUTRAL
        if filter_id == "circuit_status":
            status = context.live_snapshot.get("status", context.stock_metadata.get("status", "NORMAL"))
            allowed = params.get("allowed", "NORMAL")
            return status == allowed, status, allowed, f"market status={status}", Direction.NEUTRAL
        raise ValueError(f"no evaluator for filter {filter_id}")

    @staticmethod
    def _result(
        condition: FilterCondition,
        context: EvaluationContext,
        status: FilterStatus,
        passed: bool,
        *,
        actual_value: Any = None,
        threshold: Any = None,
        reason: str = "",
        direction: Direction = Direction.NEUTRAL,
        required_history=None,
        data_timestamp=None,
    ) -> FilterResult:
        return FilterResult(
            filter_id=condition.filter_id,
            status=status,
            passed=passed,
            score=1.0 if passed else 0.0,
            symbol=context.symbol,
            timeframe=condition.timeframe,
            actual_value=actual_value,
            threshold=threshold,
            direction=direction,
            reason=reason,
            required_history=required_history,
            data_timestamp=data_timestamp,
            metadata={"required": condition.required, "weight": condition.weight},
        )

    def _combine(self, operator: GroupOperator, children: list[FilterResult], context: EvaluationContext, node) -> FilterResult:
        if not children:
            return FilterResult(
                node_type="group",
                operator=operator,
                status=FilterStatus.ERROR,
                passed=False,
                score=0,
                symbol=context.symbol,
                reason="group has no evaluated children",
            )
        scores = [child.score for child in children]
        weighted_total = sum(child.score * float(child.metadata.get("weight", 1.0)) for child in children)
        weight_sum = sum(float(child.metadata.get("weight", 1.0)) for child in children)
        score = weighted_total / weight_sum if weight_sum else 0.0
        required = [child for child in children if child.metadata.get("required", True)]
        if operator is GroupOperator.ALL:
            blocking = required or children
            if any(child.status is FilterStatus.INSUFFICIENT_DATA for child in blocking):
                status, passed, reason = FilterStatus.INSUFFICIENT_DATA, False, "required data is insufficient"
            elif any(child.status is FilterStatus.ERROR for child in blocking):
                status, passed, reason = FilterStatus.ERROR, False, "one or more required conditions errored"
            elif any(child.status is FilterStatus.UNAVAILABLE for child in blocking):
                status, passed, reason = FilterStatus.UNAVAILABLE, False, "one or more required conditions are unavailable"
            elif any(child.status is not FilterStatus.PASS for child in blocking):
                status, passed, reason = FilterStatus.FAIL, False, "one or more required conditions failed"
            else:
                status, passed, reason = FilterStatus.PASS, True, "all required conditions passed"
        elif operator is GroupOperator.ANY:
            passed = any(child.passed for child in children)
            if passed:
                status, reason = FilterStatus.PASS, "at least one condition passed"
            elif all(child.status is FilterStatus.INSUFFICIENT_DATA for child in children):
                status, reason = FilterStatus.INSUFFICIENT_DATA, "all alternatives lack data"
            elif any(child.status is FilterStatus.ERROR for child in children):
                status, reason = FilterStatus.ERROR, "one or more alternatives errored"
            elif any(child.status is FilterStatus.UNAVAILABLE for child in children):
                status, reason = FilterStatus.UNAVAILABLE, "one or more alternatives are unavailable"
            else:
                status, reason = FilterStatus.FAIL, "no alternative condition passed"
        elif operator is GroupOperator.AT_LEAST_N:
            minimum = int(getattr(node, "min_count", 1) or 1)
            count = sum(child.passed for child in children)
            passed = count >= minimum
            if passed:
                status = FilterStatus.PASS
            elif any(child.status is FilterStatus.INSUFFICIENT_DATA for child in children):
                status = FilterStatus.INSUFFICIENT_DATA
            elif any(child.status is FilterStatus.ERROR for child in children):
                status = FilterStatus.ERROR
            elif any(child.status is FilterStatus.UNAVAILABLE for child in children):
                status = FilterStatus.UNAVAILABLE
            else:
                status = FilterStatus.FAIL
            reason = f"{count}/{minimum} conditions passed"
        elif operator is GroupOperator.WEIGHTED_SCORE:
            threshold = float(getattr(node, "threshold", 0.5) or 0.5)
            passed = score >= threshold
            if passed:
                status = FilterStatus.PASS
            elif any(child.status is FilterStatus.INSUFFICIENT_DATA for child in children):
                status = FilterStatus.INSUFFICIENT_DATA
            elif any(child.status is FilterStatus.ERROR for child in children):
                status = FilterStatus.ERROR
            elif any(child.status is FilterStatus.UNAVAILABLE for child in children):
                status = FilterStatus.UNAVAILABLE
            else:
                status = FilterStatus.FAIL
            reason = f"weighted score={score:.3f}, threshold={threshold:.3f}"
        else:  # NOT
            child = children[0]
            passed = not child.passed if child.status is FilterStatus.PASS or child.status is FilterStatus.FAIL else False
            if child.status is FilterStatus.INSUFFICIENT_DATA:
                status = FilterStatus.INSUFFICIENT_DATA
            elif child.status is FilterStatus.UNAVAILABLE:
                status = FilterStatus.UNAVAILABLE
            elif child.status is FilterStatus.ERROR:
                status = FilterStatus.ERROR
            else:
                status = FilterStatus.PASS if passed else FilterStatus.FAIL
            reason = "negated child condition"
        return FilterResult(
            node_type="group",
            operator=operator,
            status=status,
            passed=passed,
            score=score,
            symbol=context.symbol,
            children=children,
            reason=reason,
            metadata={"required": True, "weight": getattr(node, "weight", 1.0)},
        )
