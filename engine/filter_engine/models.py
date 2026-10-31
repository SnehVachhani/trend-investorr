"""Typed contracts for filter profiles and point-in-time evaluations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FilterStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_DATA = "insufficient_data"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class EvaluationMode(str, Enum):
    SCREEN = "screen"
    SIGNAL = "signal"
    BACKTEST = "backtest"


class Timeframe(str, Enum):
    ONE_MINUTE = "1m"
    FIVE_MINUTE = "5m"
    FIFTEEN_MINUTE = "15m"
    ONE_HOUR = "1h"
    ONE_DAY = "1d"
    ONE_WEEK = "1w"


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class ComparisonOperator(str, Enum):
    ABOVE = "above"
    BELOW = "below"
    BETWEEN = "between"
    EQUALS = "equals"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"


class GroupOperator(str, Enum):
    ALL = "ALL"
    ANY = "ANY"
    AT_LEAST_N = "AT_LEAST_N"
    WEIGHTED_SCORE = "WEIGHTED_SCORE"
    NOT = "NOT"


class ParameterSpec(BaseModel):
    """Whitelisted parameter metadata exposed to the frontend."""

    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    type: Literal["integer", "number", "boolean", "string", "enum"]
    required: bool = False
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    enum: list[str] = Field(default_factory=list)


class DataRequirement(BaseModel):
    """Minimum data required before a filter may produce a technical result."""

    model_config = ConfigDict(extra="forbid")

    interval: Timeframe
    minimum_candles: int = Field(ge=0)
    warmup: int = Field(default=0, ge=0)
    required_fields: list[str] = Field(default_factory=lambda: ["open", "high", "low", "close", "volume"])
    requires_live: bool = False
    reason: str = ""

    @property
    def total_candles(self) -> int:
        return self.minimum_candles + self.warmup


class CandlePoint(BaseModel):
    """Serializable candle representation used by the engine context."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: str | None = None
    delivery_pct: float = 0.0
    vwap: float = 0.0


class EvaluationContext(BaseModel):
    """All point-in-time inputs available to a filter evaluation."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    symbol: str
    as_of: datetime
    mode: EvaluationMode
    candles: dict[Timeframe, list[CandlePoint]] = Field(default_factory=dict)
    computed_features: dict[str, Any] = Field(default_factory=dict)
    stock_metadata: dict[str, Any] = Field(default_factory=dict)
    live_snapshot: dict[str, Any] = Field(default_factory=dict)
    index_context: dict[str, Any] = Field(default_factory=dict)
    event_flags: dict[str, Any] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    trendlines: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_point_in_time(self) -> "EvaluationContext":
        if self.mode is EvaluationMode.BACKTEST:
            for timeframe, series in self.candles.items():
                for candle in series:
                    try:
                        is_future = candle.timestamp > self.as_of
                    except TypeError:
                        is_future = candle.timestamp.replace(tzinfo=None) > self.as_of.replace(tzinfo=None)
                    if is_future:
                        raise ValueError(
                            f"candle for {timeframe.value} at {candle.timestamp.isoformat()} is after as_of"
                        )
        return self


class FilterCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["condition"] = "condition"
    filter_id: str = Field(min_length=1)
    timeframe: Timeframe = Timeframe.ONE_DAY
    params: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0)
    required: bool = True


class FilterGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["group"] = "group"
    operator: GroupOperator
    children: list["RuleNode"]
    min_count: int | None = Field(default=None, ge=1)
    threshold: float | None = Field(default=None, ge=0, le=1)
    weight: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def validate_group(self) -> "FilterGroup":
        if not self.children:
            raise ValueError("group must contain at least one child")
        if self.operator is GroupOperator.NOT and len(self.children) != 1:
            raise ValueError("NOT group must contain exactly one child")
        if self.operator is GroupOperator.AT_LEAST_N:
            if self.min_count is None or self.min_count > len(self.children):
                raise ValueError("AT_LEAST_N min_count must be within the child count")
        if self.operator is GroupOperator.WEIGHTED_SCORE:
            if any(child.weight <= 0 for child in self.children):
                raise ValueError("weighted groups require every child to have a positive weight")
            if self.threshold is None:
                self.threshold = 0.5
        return self


RuleNode = Annotated[Union[FilterCondition, FilterGroup], Field(discriminator="type")]
FilterGroup.model_rebuild()


class FilterExpression(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = Field(default=1, ge=1)
    operator: GroupOperator = GroupOperator.ALL
    children: list[RuleNode]
    veto: list[RuleNode] = Field(default_factory=list)
    minimum_score: float = Field(default=0.0, ge=0, le=1)
    maximum_insufficient_data: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_expression(self) -> "FilterExpression":
        if not self.children:
            raise ValueError("expression must contain at least one child")
        self._validate_depth(self.children + self.veto, 1)
        return self

    @classmethod
    def _validate_depth(cls, nodes: list[RuleNode], depth: int) -> None:
        if depth > 8:
            raise ValueError("expression nesting cannot exceed 8 levels")
        for node in nodes:
            if isinstance(node, FilterGroup):
                cls._validate_depth(node.children, depth + 1)


class FilterDefinition(BaseModel):
    """Catalog metadata; evaluator code is referenced only by a stable key."""

    model_config = ConfigDict(extra="forbid")

    filter_id: str
    name: str
    description: str
    category: str
    tier: int = Field(ge=1, le=6)
    supported_modes: list[EvaluationMode]
    supported_timeframes: list[Timeframe]
    parameter_schema: list[ParameterSpec]
    evaluator_key: str
    history_rule: str
    default_timeframe: Timeframe = Timeframe.ONE_DAY
    requires_live: bool = False
    availability: Literal["available", "planned"] = "available"


class FilterResult(BaseModel):
    """Explainable result for an atomic condition or composed group."""

    model_config = ConfigDict(extra="forbid")

    node_type: Literal["condition", "group", "expression"] = "condition"
    filter_id: str | None = None
    operator: GroupOperator | None = None
    status: FilterStatus
    passed: bool
    score: float = Field(default=0.0, ge=0, le=1)
    symbol: str
    timeframe: Timeframe | None = None
    actual_value: Any = None
    threshold: Any = None
    direction: Direction = Direction.NEUTRAL
    reason: str = ""
    data_timestamp: datetime | None = None
    required_history: DataRequirement | None = None
    children: list["FilterResult"] = Field(default_factory=list)
    veto_results: list["FilterResult"] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


FilterResult.model_rebuild()
