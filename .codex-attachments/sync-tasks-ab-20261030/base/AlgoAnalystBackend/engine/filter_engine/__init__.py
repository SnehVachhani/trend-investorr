"""Reusable, explainable technical filter and rule evaluation engine."""

from .catalog import FilterCatalog, build_default_catalog
from .evaluator import FilterEvaluator
from .models import (
    EvaluationContext,
    EvaluationMode,
    FilterExpression,
    FilterResult,
    FilterStatus,
    Timeframe,
)

__all__ = [
    "EvaluationContext",
    "EvaluationMode",
    "FilterCatalog",
    "FilterEvaluator",
    "FilterExpression",
    "FilterResult",
    "FilterStatus",
    "Timeframe",
    "build_default_catalog",
]
