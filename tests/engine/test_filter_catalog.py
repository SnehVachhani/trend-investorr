"""Catalog and requirement tests for the reusable filter engine."""

import pytest
from engine.filter_engine.catalog import FilterCatalog, build_default_catalog
from engine.filter_engine.models import EvaluationMode, FilterExpression, Timeframe


def test_default_catalog_contains_wave_a_filters():
    catalog = build_default_catalog()

    for filter_id in (
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
        "price_above_vwap",
        "relative_strength",
        "breakout",
        "event_risk",
        "circuit_status",
    ):
        definition = catalog.get(filter_id)
        assert definition.filter_id == filter_id
        assert definition.parameter_schema
        assert definition.supported_modes
        assert definition.supported_timeframes


def test_catalog_resolves_parameterized_history_requirements():
    catalog = build_default_catalog()

    requirement = catalog.requirements_for(
        "price_above_sma",
        {"period": 200},
        timeframe=Timeframe.ONE_DAY,
    )

    assert requirement.interval is Timeframe.ONE_DAY
    assert requirement.minimum_candles >= 200
    assert requirement.warmup >= 0


def test_catalog_rejects_unknown_filter_id():
    catalog = build_default_catalog()

    with pytest.raises(KeyError, match="unknown filter"):
        catalog.get("does_not_exist")


def test_expression_validation_rejects_unknown_filter_and_unsupported_mode():
    catalog = build_default_catalog()
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "does_not_exist",
                    "timeframe": "1d",
                }
            ],
        }
    )

    with pytest.raises(KeyError, match="unknown filter"):
        catalog.validate_expression(expression, EvaluationMode.SIGNAL)


def test_catalog_filter_metadata_is_json_serializable():
    catalog = FilterCatalog(build_default_catalog().definitions)
    payload = [item.model_dump(mode="json") for item in catalog.list()]

    assert payload
    assert all(item["filter_id"] for item in payload)
    assert all(item["parameter_schema"] for item in payload if item["availability"] == "available")


def test_catalog_exposes_roadmap_filters_without_making_them_executable():
    catalog = build_default_catalog()

    planned = catalog.get("bollinger_width")

    assert planned.availability == "planned"
    assert planned.supported_modes == []
    with pytest.raises(ValueError, match="does not support mode"):
        catalog.validate_expression(
            FilterExpression.model_validate(
                {
                    "operator": "ALL",
                    "children": [{"type": "condition", "filter_id": "bollinger_width", "timeframe": "1d"}],
                }
            ),
            EvaluationMode.SCREEN,
        )


def test_catalog_rejects_contradictory_filter_ranges():
    catalog = build_default_catalog()
    expression = FilterExpression.model_validate(
        {
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "rsi_range",
                    "timeframe": "1d",
                    "params": {"period": 14, "minimum": 80, "maximum": 20},
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="less than or equal"):
        catalog.validate_expression(expression, EvaluationMode.BACKTEST)
