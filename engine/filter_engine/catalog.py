"""Whitelisted, serializable catalog of reusable technical filters."""

from __future__ import annotations

from typing import Any, Iterable

from .models import (
    EvaluationMode,
    FilterDefinition,
    FilterExpression,
    FilterGroup,
    ParameterSpec,
    RuleNode,
    Timeframe,
)
from .requirements import history_for


class FilterCatalog:
    """Registry used by both the API and evaluator; no dynamic code is accepted."""

    def __init__(self, definitions: Iterable[FilterDefinition]):
        self.definitions = list(definitions)
        self._by_id = {definition.filter_id: definition for definition in self.definitions}
        if len(self._by_id) != len(self.definitions):
            raise ValueError("filter catalog contains duplicate filter IDs")

    def list(self) -> list[FilterDefinition]:
        return sorted(self.definitions, key=lambda item: (item.tier, item.category, item.filter_id))

    def get(self, filter_id: str) -> FilterDefinition:
        try:
            return self._by_id[filter_id]
        except KeyError as exc:
            raise KeyError(f"unknown filter: {filter_id}") from exc

    def requirements_for(
        self,
        filter_id: str,
        params: dict[str, Any] | None = None,
        *,
        timeframe: Timeframe | None = None,
    ):
        definition = self.get(filter_id)
        selected = timeframe or definition.default_timeframe
        if selected not in definition.supported_timeframes:
            raise ValueError(
                f"filter {filter_id} does not support timeframe {selected.value}"
            )
        return history_for(filter_id, params or {}, selected)

    def validate_expression(self, expression: FilterExpression, mode: EvaluationMode) -> None:
        for node in expression.children + expression.veto:
            self._validate_node(node, mode)

    def _validate_node(self, node: RuleNode, mode: EvaluationMode) -> None:
        if isinstance(node, FilterGroup):
            for child in node.children:
                self._validate_node(child, mode)
            return
        definition = self.get(node.filter_id)
        if mode not in definition.supported_modes:
            raise ValueError(f"filter {node.filter_id} does not support mode {mode.value}")
        if node.timeframe not in definition.supported_timeframes:
            raise ValueError(
                f"filter {node.filter_id} does not support timeframe {node.timeframe.value}"
            )
        self._validate_parameters(node.filter_id, definition.parameter_schema, node.params)

    @staticmethod
    def _validate_parameters(filter_id: str, schema: list[ParameterSpec], params: dict[str, Any]) -> None:
        known = {item.name for item in schema}
        unknown = sorted(set(params) - known)
        if unknown:
            raise ValueError(f"unknown filter parameter(s): {', '.join(unknown)}")
        for item in schema:
            if item.required and item.name not in params and item.default is None:
                raise ValueError(f"missing required filter parameter: {item.name}")
            value = params.get(item.name, item.default)
            if value is None:
                continue
            if item.type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"parameter {item.name} must be an integer")
            if item.type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"parameter {item.name} must be a number")
            if item.minimum is not None and float(value) < item.minimum:
                raise ValueError(f"parameter {item.name} must be >= {item.minimum}")
            if item.maximum is not None and float(value) > item.maximum:
                raise ValueError(f"parameter {item.name} must be <= {item.maximum}")
            if item.type == "enum" and item.enum and value not in item.enum:
                raise ValueError(f"parameter {item.name} must be one of {item.enum}")

        # These ranges are mathematically valid JSON but semantically
        # contradictory. Reject them at profile creation time instead of
        # allowing a filter that can never express the user's intent.
        if filter_id in {"price_range", "rsi_range", "atr_percent"}:
            minimum = params.get("min_price" if filter_id == "price_range" else "minimum")
            maximum = params.get("max_price" if filter_id == "price_range" else "maximum")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("minimum must be less than or equal to maximum")
        if filter_id == "ema_relation":
            fast = params.get("fast_period", 20)
            slow = params.get("slow_period", 50)
            if fast >= slow:
                raise ValueError("fast_period must be less than slow_period")
        if filter_id == "bullish_swing_poc_pullback":
            lookback = params.get("lookback", 250)
            pivot_window = params.get("pivot_window", 3)
            if lookback < pivot_window * 2 + 5:
                raise ValueError("lookback must be at least twice pivot_window plus five candles")


def _common_modes() -> list[EvaluationMode]:
    return [EvaluationMode.SCREEN, EvaluationMode.SIGNAL, EvaluationMode.BACKTEST]


_OHLCV_TIMEFRAMES = [
    Timeframe.ONE_MINUTE,
    Timeframe.FIVE_MINUTE,
    Timeframe.FIFTEEN_MINUTE,
    Timeframe.ONE_HOUR,
    Timeframe.ONE_DAY,
]


def _condition(
    filter_id: str,
    name: str,
    category: str,
    tier: int,
    evaluator_key: str,
    *,
    description: str,
    parameters: list[ParameterSpec],
    timeframes: list[Timeframe] | None = None,
    modes: list[EvaluationMode] | None = None,
    history_rule: str = "dynamic",
    requires_live: bool = False,
    availability: str = "available",
    default_timeframe: Timeframe = Timeframe.ONE_DAY,
) -> FilterDefinition:
    return FilterDefinition(
        filter_id=filter_id,
        name=name,
        description=description,
        category=category,
        tier=tier,
        supported_modes=_common_modes() if modes is None else modes,
        # Any implemented OHLCV filter can be evaluated on intraday candles.
        # Keep 1d as the default timeframe, while allowing the dedicated
        # 5-minute execution presets to validate and run correctly.
        supported_timeframes=timeframes or _OHLCV_TIMEFRAMES,
        parameter_schema=parameters,
        evaluator_key=evaluator_key,
        history_rule=history_rule,
        requires_live=requires_live,
        availability=availability,
        default_timeframe=default_timeframe,
    )


def _planned_condition(
    filter_id: str,
    name: str,
    category: str,
    tier: int,
    *,
    description: str,
    timeframe: Timeframe = Timeframe.ONE_DAY,
) -> FilterDefinition:
    """Expose the roadmap without pretending an evaluator is executable."""
    return _condition(
        filter_id,
        name,
        category,
        tier,
        "planned",
        description=description,
        parameters=[],
        timeframes=[timeframe],
        modes=[],
        availability="planned",
        default_timeframe=timeframe,
    )


def build_default_catalog() -> FilterCatalog:
    number = lambda name, label, default=None, minimum=None, maximum=None, required=False: ParameterSpec(
        name=name, label=label, type="number", default=default, minimum=minimum, maximum=maximum, required=required
    )
    integer = lambda name, label, default=None, minimum=None, maximum=None, required=False: ParameterSpec(
        name=name, label=label, type="integer", default=default, minimum=minimum, maximum=maximum, required=required
    )
    enum = lambda name, label, values, default=None: ParameterSpec(
        name=name, label=label, type="enum", enum=values, default=default
    )
    definitions = [
        _condition("data_completeness", "Data completeness", "eligibility", 1, "data_completeness", description="Reject missing or insufficient OHLCV history.", parameters=[integer("minimum_candles", "Minimum candles", 30, 1)]),
        _condition("price_range", "Price range", "eligibility", 1, "price_range", description="Keep stocks inside a configured price range.", parameters=[number("min_price", "Minimum price", 0, 0), number("max_price", "Maximum price", None, 0)]),
        _condition("average_volume", "Average volume", "liquidity", 1, "average_volume", description="Require average volume over a lookback.", parameters=[integer("period", "Period", 30, 1), number("minimum", "Minimum average volume", 0, 0)]),
        _condition("average_traded_value", "Average traded value", "liquidity", 1, "average_traded_value", description="Require average close multiplied by volume.", parameters=[integer("period", "Period", 30, 1), number("minimum", "Minimum traded value", 0, 0)]),
        _condition("price_above_sma", "Price versus SMA", "trend", 1, "price_above_sma", description="Compare the latest close with a simple moving average.", parameters=[integer("period", "Period", 200, 1), enum("relation", "Relation", ["above", "below"], "above")]),
        _condition("ema_relation", "EMA relationship", "trend", 1, "ema_relation", description="Compare fast and slow exponential moving averages.", parameters=[integer("fast_period", "Fast period", 20, 1), integer("slow_period", "Slow period", 50, 1), enum("relation", "Relation", ["above", "below"], "above")]),
        _condition("rsi_range", "RSI range", "momentum", 2, "rsi_range", description="Keep RSI between configured bounds.", parameters=[integer("period", "Period", 14, 2), number("minimum", "Minimum RSI", 40, 0, 100), number("maximum", "Maximum RSI", 70, 0, 100)]),
        _condition("macd_direction", "MACD direction", "momentum", 2, "macd_direction", description="Require MACD line to be above or below its signal.", parameters=[enum("direction", "Direction", ["bullish", "bearish"], "bullish")]),
        _condition("atr_percent", "ATR percentage", "volatility", 1, "atr_percent", description="Constrain ATR as a percentage of price.", parameters=[integer("period", "Period", 14, 2), number("minimum", "Minimum ATR %", 0, 0), number("maximum", "Maximum ATR %", 10, 0)]),
        _condition("adx_threshold", "ADX threshold", "trend", 2, "adx_threshold", description="Require a minimum trend-strength reading.", parameters=[integer("period", "Period", 14, 2), number("minimum", "Minimum ADX", 20, 0, 100)]),
        _condition("relative_volume", "Relative volume", "volume", 1, "relative_volume", description="Compare latest volume to a prior average.", parameters=[integer("period", "Period", 20, 2), number("multiple", "Minimum multiple", 1.5, 0)]),
        _condition("bullish_swing_poc_pullback", "Bullish swing POC pullback", "structure", 1, "bullish_swing_poc_pullback", description="Require confirmed higher highs and higher lows, then a bullish rejection from the latest swing's volume-profile value area. Volume at price is estimated from OHLCV candles.", parameters=[integer("lookback", "Swing lookback", 250, 20, 5000), integer("pivot_window", "Pivot confirmation bars", 3, 1, 20), number("value_area_percent", "Value area volume %", 70, 1, 100), number("zone_buffer_pct", "Value area buffer % of swing", 0.25, 0, 10)]),
        _condition("price_above_vwap", "Price versus VWAP", "intraday", 2, "price_above_vwap", description="Require price above or below session VWAP/live VWAP.", parameters=[enum("relation", "Relation", ["above", "below"], "above")], timeframes=[Timeframe.ONE_MINUTE, Timeframe.FIVE_MINUTE, Timeframe.FIFTEEN_MINUTE, Timeframe.ONE_HOUR, Timeframe.ONE_DAY], requires_live=True),
        _condition("previous_level", "Previous candle level", "levels", 2, "previous_level", description="Compare price with a previous high, low, or close.", parameters=[enum("level", "Level", ["high", "low", "close"], "high"), enum("relation", "Relation", ["above", "below"], "above")]),
        _condition("support_resistance", "Support/resistance distance", "levels", 1, "support_resistance", description="Require price to be a configured distance from swing levels.", parameters=[enum("level", "Level", ["support", "resistance"], "support"), number("minimum_distance_pct", "Minimum distance %", 0, 0)]),
        _condition("relative_strength", "Relative strength", "breadth", 1, "relative_strength", description="Compare stock return with index or sector return.", parameters=[number("minimum_excess_return_pct", "Minimum excess return %", 0)]),
        _condition("breakout", "Breakout/breakdown", "structure", 1, "breakout", description="Detect a close above or below a prior range.", parameters=[integer("lookback", "Lookback", 20, 2), enum("direction", "Direction", ["bullish", "bearish"], "bullish"), number("volume_multiple", "Volume multiple", 0, 0)]),
        _condition("trendline_break", "Trendline break", "structure", 1, "trendline_break", description="Compare price with a manually drawn symbol-specific trendline.", parameters=[integer("trendline_id", "Trendline ID", minimum=1, required=True), enum("direction", "Direction", ["breaks_above", "breaks_below"], "breaks_above"), number("buffer_pct", "Break buffer %", 0, 0)]),
        _condition("event_risk", "Event risk", "risk", 1, "event_risk", description="Block entries when configured event flags are active.", parameters=[number("days_ahead", "Days ahead", 2, 0),]),
        _condition("circuit_status", "Circuit/suspension status", "risk", 1, "circuit_status", description="Require a tradable, non-suspended quote state.", parameters=[enum("allowed", "Allowed status", ["NORMAL"], "NORMAL")], requires_live=True),
    ]
    # These are part of the product's filter roadmap. They are returned by the
    # catalog so the UI is complete, but supported_modes=[] keeps them from
    # being saved, previewed, published, or executed until an evaluator exists.
    planned = [
        ("market_cap", "Market capitalization", "eligibility", 1, "Require a minimum or maximum market capitalization."),
        ("exchange_series", "Exchange and series", "eligibility", 1, "Restrict symbols by exchange and equity series."),
        ("spread", "Bid-ask spread", "liquidity", 1, "Limit the current bid-ask spread."),
        ("slippage", "Estimated slippage", "liquidity", 1, "Reject setups whose estimated execution slippage is too high."),
        ("surveillance_status", "Surveillance status", "risk", 1, "Exclude symbols under surveillance or trade restrictions."),
        ("sma_alignment", "Moving-average alignment", "trend", 1, "Require several moving averages to be ordered in trend direction."),
        ("higher_highs_lows", "Higher highs and lows", "trend", 1, "Detect rising or falling swing structure."),
        ("supertrend_direction", "Supertrend direction", "trend", 1, "Require Supertrend bullish or bearish direction."),
        ("multi_timeframe_alignment", "Multi-timeframe alignment", "trend", 1, "Require a trend condition to agree across timeframes."),
        ("index_alignment", "Index alignment", "breadth", 1, "Require the stock trend to agree with its benchmark index."),
        ("sector_alignment", "Sector alignment", "breadth", 1, "Require the stock trend to agree with its sector."),
        ("rsi_cross", "RSI threshold cross", "momentum", 2, "Detect RSI crossing a configured threshold."),
        ("macd_crossover", "MACD crossover", "momentum", 2, "Detect MACD crossing its signal line."),
        ("macd_histogram", "MACD histogram", "momentum", 2, "Constrain MACD histogram direction or magnitude."),
        ("rate_of_change", "Rate of change", "momentum", 2, "Require price rate of change above or below a threshold."),
        ("price_momentum", "Price momentum", "momentum", 2, "Compare current price with a prior lookback price."),
        ("momentum_divergence", "Momentum divergence", "momentum", 2, "Detect price and oscillator divergence."),
        ("stochastic_range", "Stochastic range", "momentum", 2, "Keep stochastic oscillator inside configured bounds."),
        ("cci_range", "CCI range", "momentum", 2, "Keep commodity channel index inside configured bounds."),
        ("williams_r", "Williams %R", "momentum", 2, "Constrain Williams %R over a lookback."),
        ("bollinger_band_position", "Bollinger band position", "volatility", 1, "Require price position within Bollinger bands."),
        ("bollinger_width", "Bollinger width", "volatility", 1, "Require a minimum or maximum Bollinger width."),
        ("volatility_expansion", "Volatility expansion", "volatility", 1, "Detect an increase in true range or ATR."),
        ("volatility_contraction", "Volatility contraction", "volatility", 1, "Detect a volatility squeeze or contraction."),
        ("range_expansion", "Range expansion", "volatility", 1, "Require the latest range to exceed its recent average."),
        ("reward_risk", "Reward-to-risk", "risk", 1, "Require a configured minimum reward-to-risk ratio."),
        ("volume_confirmation", "Volume confirmation", "volume", 1, "Require volume to confirm a price move."),
        ("obv_trend", "OBV trend", "volume", 2, "Require on-balance volume trend direction."),
        ("cmf_threshold", "Chaikin money flow", "volume", 2, "Require Chaikin money flow above or below a threshold."),
        ("accumulation_distribution", "Accumulation/distribution", "volume", 2, "Detect accumulation or distribution pressure."),
        ("delivery_percent", "Delivery percentage", "volume", 1, "Require delivery percentage in a configured range."),
        ("delivery_vs_average", "Delivery versus average", "volume", 1, "Compare delivery percentage with its historical average."),
        ("pivot_level", "Pivot level", "levels", 2, "Compare price with a daily pivot level."),
        ("weekly_level", "Weekly level", "levels", 2, "Compare price with a prior weekly high, low, or close."),
        ("monthly_level", "Monthly level", "levels", 2, "Compare price with a prior monthly high, low, or close."),
        ("breakout_retest", "Breakout retest", "structure", 1, "Detect a successful retest after a breakout."),
        ("reclaim_level", "Level reclaim", "structure", 1, "Detect price reclaiming a broken support or resistance level."),
        ("rejection_wick", "Rejection wick", "structure", 2, "Detect rejection of a configured level."),
        ("consolidation_range", "Consolidation range", "structure", 1, "Detect a bounded consolidation or base."),
        ("inside_bar", "Inside bar", "structure", 2, "Detect an inside-bar pattern."),
        ("gap", "Gap", "intraday", 2, "Detect an opening gap above or below a threshold."),
        ("anchored_vwap", "Anchored VWAP", "intraday", 2, "Compare price with an anchored VWAP."),
        ("opening_range_breakout", "Opening range breakout", "intraday", 1, "Detect a break of the opening range."),
        ("intraday_high_low_break", "Intraday high/low break", "intraday", 1, "Detect a break of the current session high or low."),
        ("gap_fill", "Gap fill", "intraday", 2, "Detect a partial or complete gap fill."),
        ("orderbook_spread", "Order-book spread", "intraday", 1, "Require live order-book spread within a limit.", Timeframe.FIVE_MINUTE),
        ("orderflow_imbalance", "Order-flow imbalance", "intraday", 2, "Require live bid/ask flow imbalance.", Timeframe.FIVE_MINUTE),
        ("market_breadth", "Market breadth", "breadth", 1, "Require a configured market breadth condition."),
        ("advance_decline", "Advance/decline", "breadth", 1, "Require advance/decline context."),
        ("sector_breadth", "Sector breadth", "breadth", 1, "Require sector breadth context."),
        ("news_risk", "News risk", "risk", 1, "Block entries when configured news risk is active."),
        ("earnings_event", "Earnings event", "risk", 1, "Block or allow entries around earnings events."),
        ("manual_horizontal_level", "Manual horizontal level", "charting", 1, "Compare price with a symbol-specific drawn level."),
        ("pivot_trendline", "Pivot-derived trendline", "charting", 1, "Compare price with a generalized pivot-derived trendline."),
    ]
    definitions.extend(
        _planned_condition(filter_id, name, category, tier, description=description, timeframe=timeframe)
        for item in planned
        for filter_id, name, category, tier, description, *timeframe_value in [item]
        for timeframe in [timeframe_value[0] if timeframe_value else Timeframe.ONE_DAY]
    )
    return FilterCatalog(definitions)
