"""Composition evaluator tests for reusable filter profiles."""

from datetime import datetime, timedelta, timezone

import pytest

from engine.filter_engine.context import build_evaluation_context, build_fast_backtest_context
from engine.filter_engine.catalog import build_default_catalog
from engine.filter_engine.backtest_cache import build_atomic_backtest_cache
from engine.filter_engine.evaluator import FilterEvaluator
from engine.filter_engine.models import (
    EvaluationMode,
    FilterCondition,
    FilterExpression,
    FilterStatus,
    Timeframe,
    CandlePoint,
)
from engine.filter_engine.presets import build_filter_presets
from tests.factories import make_candle_series


def _expression(operator="ALL"):
    return FilterExpression.model_validate(
        {
            "operator": operator,
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_above_sma",
                    "timeframe": "1d",
                    "params": {"period": 20},
                    "required": True,
                },
                {
                    "type": "condition",
                    "filter_id": "relative_volume",
                    "timeframe": "1d",
                    "params": {"multiple": 1.2},
                    "required": False,
                    "weight": 2,
                },
            ],
        }
    )


def test_all_expression_returns_pass_and_explanations():
    candles = make_candle_series(count=60, base_price=100, daily_change=2)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.SCREEN,
        candles={Timeframe.ONE_DAY: candles},
    )

    result = FilterEvaluator().evaluate(_expression(), context)

    assert result.status is FilterStatus.PASS
    assert result.passed is True
    assert len(result.children) == 2
    assert result.children[0].filter_id == "price_above_sma"
    assert result.children[0].actual_value is not None


def test_required_failure_blocks_all_group():
    candles = make_candle_series(count=60, base_price=100, daily_change=-2)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.SCREEN,
        candles={Timeframe.ONE_DAY: candles},
    )

    result = FilterEvaluator().evaluate(_expression(), context)

    assert result.status is FilterStatus.FAIL
    assert result.passed is False
    assert any(not child.passed for child in result.children)


def test_insufficient_history_is_not_a_normal_failure():
    candles = make_candle_series(count=10, base_price=100, daily_change=1)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.SCREEN,
        candles={Timeframe.ONE_DAY: candles},
    )

    result = FilterEvaluator().evaluate(_expression(), context)

    assert result.status is FilterStatus.INSUFFICIENT_DATA
    assert result.passed is False
    assert result.children[0].status is FilterStatus.INSUFFICIENT_DATA


def test_expression_enforces_maximum_insufficient_data():
    expression = _expression()
    expression.maximum_insufficient_data = 1
    candles = make_candle_series(count=10, base_price=100, daily_change=1)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.SCREEN,
        candles={Timeframe.ONE_DAY: candles},
    )

    result = FilterEvaluator().evaluate(expression, context)

    assert result.status is FilterStatus.INSUFFICIENT_DATA
    assert "maximum allowed is 1" in result.reason


def test_veto_rejects_even_when_main_expression_passes():
    candles = make_candle_series(count=60, base_price=100, daily_change=2)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.SCREEN,
        candles={Timeframe.ONE_DAY: candles},
        event_flags={"event_risk": True},
    )
    expression = _expression()
    expression.veto = [
        {
            "type": "condition",
            "filter_id": "event_risk",
            "params": {"days_ahead": 2},
        }
    ]

    result = FilterEvaluator().evaluate(expression, context)

    assert result.status is FilterStatus.FAIL
    assert result.passed is False
    assert result.veto_results[0].status == FilterStatus.FAIL


def test_historical_vwap_resets_at_the_session_boundary():
    start = datetime(2024, 1, 2, 9, 15, tzinfo=timezone.utc)
    candles = []
    for index, (day, close, volume) in enumerate(
        [(0, 100, 10_000), (0, 100, 10_000), (1, 110, 100), (1, 109, 100)]
    ):
        candles.append(
            {
                "symbol": "RELIANCE",
                "timestamp": start + timedelta(days=day, minutes=index * 5),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
            }
        )
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_above_vwap",
                    "timeframe": "5m",
                    "params": {"relation": "above"},
                }
            ],
        }
    )
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1]["timestamp"],
        mode=EvaluationMode.BACKTEST,
        candles={Timeframe.FIVE_MINUTE: candles},
    )

    result = FilterEvaluator().evaluate(expression, context)

    assert result.status is FilterStatus.FAIL
    assert result.children[0].actual_value == 109.0


def test_external_context_filters_do_not_silently_pass_without_context():
    candles = make_candle_series(count=10, base_price=100, daily_change=1)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.BACKTEST,
        candles={Timeframe.ONE_DAY: candles},
    )
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {"type": "condition", "filter_id": "event_risk", "timeframe": "1d"},
                {"type": "condition", "filter_id": "relative_strength", "timeframe": "1d"},
            ],
        }
    )

    result = FilterEvaluator().evaluate(expression, context)

    assert all(child.status is FilterStatus.UNAVAILABLE for child in result.children)
    assert result.status is FilterStatus.UNAVAILABLE


def test_veto_insufficient_data_counts_toward_expression_limit():
    expression = _expression()
    expression.maximum_insufficient_data = 0
    expression.veto = [
        {
            "type": "condition",
            "filter_id": "price_above_sma",
            "timeframe": "1d",
            "params": {"period": 50},
        }
    ]
    candles = make_candle_series(count=10, base_price=100, daily_change=1)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.BACKTEST,
        candles={Timeframe.ONE_DAY: candles},
    )

    result = FilterEvaluator().evaluate(expression, context)

    assert result.status is FilterStatus.INSUFFICIENT_DATA
    assert result.veto_results[0].status is FilterStatus.INSUFFICIENT_DATA


def test_trendline_break_uses_symbol_specific_drawn_points():
    candles = make_candle_series(count=5, base_price=100, daily_change=2)
    expression = FilterExpression(
        children=[FilterCondition(
            filter_id="trendline_break",
            timeframe=Timeframe.ONE_DAY,
            params={"trendline_id": 7, "direction": "breaks_above"},
        )]
    )
    context = build_evaluation_context(
        symbol="TCS",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.SCREEN,
        candles={Timeframe.ONE_DAY: candles},
        trendlines=[{"id": 7, "points_json": [
            {"time": candles[0].timestamp.timestamp(), "price": 116},
            {"time": candles[-1].timestamp.timestamp(), "price": 117.5},
        ]}],
    )

    result = FilterEvaluator(build_default_catalog()).evaluate(expression, context)

    assert result.status == FilterStatus.PASS
    assert result.children[0].threshold == 117.5


def test_research_presets_execute_without_runtime_errors_in_backtest():
    candles = make_candle_series(count=300, base_price=100, daily_change=1)
    context = build_evaluation_context(
        symbol="RELIANCE",
        as_of=candles[-1].timestamp,
        mode=EvaluationMode.BACKTEST,
        candles={Timeframe.ONE_DAY: candles},
    )
    research_ids = {
        "minervini_trend_leader",
        "oneil_growth_breakout",
        "weinstein_stage2_approx",
        "connors_rsi2_pullback",
        "ema_crossback_pullback",
        "trend_strength_baseline",
    }

    presets = [preset for preset in build_filter_presets() if preset.preset_id in research_ids]

    assert {preset.preset_id for preset in presets} == research_ids
    for preset in presets:
        result = FilterEvaluator().evaluate(preset.expression, context)
        assert result.status is not FilterStatus.ERROR
        assert all(child.status is not FilterStatus.ERROR for child in result.children)


def test_shared_backtest_cache_matches_reference_evaluator():
    raw_candles = make_candle_series(count=120, base_price=100, daily_change=0.4)
    candles = [
        CandlePoint(
            symbol=str(candle.symbol),
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )
        for candle in raw_candles
    ]
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {"type": "condition", "filter_id": "rsi_range", "timeframe": "1d", "params": {"period": 14, "minimum": 40, "maximum": 80}},
                {"type": "condition", "filter_id": "ema_relation", "timeframe": "1d", "params": {"fast_period": 20, "slow_period": 50, "relation": "above"}},
                {"type": "condition", "filter_id": "atr_percent", "timeframe": "1d", "params": {"period": 14, "minimum": 0, "maximum": 20}},
            ],
        }
    )
    catalog = build_default_catalog()
    cache = build_atomic_backtest_cache([expression], {Timeframe.ONE_DAY: candles}, catalog)
    reference = FilterEvaluator(catalog)
    cached = FilterEvaluator(catalog, atomic_cache=cache)

    for index in range(59, len(candles)):
        window = candles[index - 59:index + 1]
        context = build_fast_backtest_context(
            symbol="RELIANCE",
            as_of=candles[index].timestamp,
            candles={Timeframe.ONE_DAY: window},
        )
        expected = reference.evaluate(expression, context)
        actual = cached.evaluate(expression, context)
        assert actual.status == expected.status
        assert actual.passed == expected.passed
        for actual_child, expected_child in zip(actual.children, expected.children):
            assert actual_child.status == expected_child.status
            assert actual_child.passed == expected_child.passed
            assert actual_child.reason == expected_child.reason
            if isinstance(expected_child.actual_value, dict):
                assert actual_child.actual_value.keys() == expected_child.actual_value.keys()
                for key in expected_child.actual_value:
                    assert actual_child.actual_value[key] == pytest.approx(expected_child.actual_value[key])
            elif isinstance(expected_child.actual_value, (int, float)):
                assert actual_child.actual_value == pytest.approx(expected_child.actual_value)
