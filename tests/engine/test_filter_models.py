"""Contract tests for the modular filter engine domain models."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from engine.filter_engine.models import (
    EvaluationContext,
    EvaluationMode,
    FilterExpression,
    FilterGroup,
    FilterStatus,
    GroupOperator,
    Timeframe,
)


def test_expression_round_trips_with_stable_schema():
    expression = FilterExpression.model_validate(
        {
            "schema_version": 1,
            "operator": "ALL",
            "children": [
                {
                    "type": "condition",
                    "filter_id": "price_above_sma",
                    "timeframe": "1d",
                    "params": {"period": 200},
                    "required": True,
                },
                {
                    "type": "group",
                    "operator": "ANY",
                    "children": [
                        {
                            "type": "condition",
                            "filter_id": "relative_volume",
                            "timeframe": "15m",
                            "params": {"multiple": 1.5},
                        }
                    ],
                },
            ],
            "veto": [],
        }
    )

    payload = expression.model_dump(mode="json")

    assert payload["schema_version"] == 1
    assert payload["operator"] == "ALL"
    assert payload["children"][0]["filter_id"] == "price_above_sma"
    assert payload["children"][1]["operator"] == "ANY"
    assert expression.children[0].timeframe is Timeframe.ONE_DAY


def test_empty_group_is_rejected():
    with pytest.raises(ValidationError, match="at least one child"):
        FilterGroup(operator=GroupOperator.ALL, children=[])


def test_invalid_weighted_group_is_rejected():
    with pytest.raises(ValidationError, match="weighted"):
        FilterGroup(
            operator=GroupOperator.WEIGHTED_SCORE,
            children=[
                {
                    "type": "condition",
                    "filter_id": "relative_volume",
                    "weight": 0,
                }
            ],
        )


def test_context_rejects_future_candles_for_backtest():
    with pytest.raises(ValidationError, match="as_of"):
        EvaluationContext(
            symbol="RELIANCE",
            as_of=datetime(2026, 1, 2),
            mode=EvaluationMode.BACKTEST,
            candles={
                Timeframe.ONE_DAY: [
                    {
                        "symbol": "RELIANCE",
                        "timestamp": datetime(2026, 1, 3),
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                        "volume": 1000,
                        "interval": "ONE_DAY",
                    }
                ]
            },
        )


def test_filter_status_distinguishes_missing_data():
    assert FilterStatus.INSUFFICIENT_DATA.value == "insufficient_data"
    assert FilterStatus.UNAVAILABLE.value == "unavailable"
