"""Safe comparison helpers shared by atomic filters."""

from __future__ import annotations

from typing import Any

from .models import ComparisonOperator


def compare(actual: float, operator: ComparisonOperator | str, expected: Any) -> bool:
    """Compare numeric values without treating missing values as false signals."""
    if actual is None or expected is None:
        raise ValueError("comparison requires actual and expected values")
    op = ComparisonOperator(operator)
    if op is ComparisonOperator.ABOVE:
        return actual > float(expected)
    if op is ComparisonOperator.BELOW:
        return actual < float(expected)
    if op is ComparisonOperator.BETWEEN:
        low, high = expected
        return float(low) <= actual <= float(high)
    if op is ComparisonOperator.EQUALS:
        return actual == float(expected)
    raise ValueError(f"{op.value} requires a previous value or a series")


def as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
