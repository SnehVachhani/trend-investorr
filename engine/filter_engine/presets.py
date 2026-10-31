"""Curated, runnable multi-timeframe filter combinations.

The presets use 5-minute bars as their execution/signal clock.  Longer-term
trend and liquidity checks remain on daily candles where that preserves the
meaning of the original swing-trading rule.  This avoids the common mistake
of treating ``SMA(200)`` on five-minute bars as a 200-day trend.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .models import FilterExpression, Timeframe


class FilterPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_id: str
    name: str
    description: str
    category: str
    # The bar on which entries, exits, and the backtest loop are simulated.
    timeframe: Timeframe = Timeframe.FIVE_MINUTE
    holding_period: str
    max_hold_bars: int
    default_stop_loss_pct: float
    default_take_profit_pct: float
    expression: FilterExpression


INTRADAY_BARS_PER_TRADING_DAY = 75


def _bars_for_trading_days(days: int) -> int:
    """Convert a human trading-day hold period to 5-minute bars."""
    return days * INTRADAY_BARS_PER_TRADING_DAY


def _condition(
    filter_id: str,
    params: dict,
    *,
    required: bool = True,
    timeframe: Timeframe = Timeframe.FIVE_MINUTE,
) -> dict:
    return {
        "type": "condition",
        "filter_id": filter_id,
        "timeframe": timeframe.value,
        "params": params,
        "weight": 1,
        "required": required,
    }


def _expression(children: list[dict]) -> FilterExpression:
    return FilterExpression.model_validate(
        {
            "schema_version": 1,
            "operator": "ALL",
            "children": children,
            "veto": [],
            "minimum_score": 0,
            "maximum_insufficient_data": 0,
        }
    )


def _shared_gates(minimum_candles: int, *, traded_value: float = 10_000_000) -> list[dict]:
    """Add backtestable data and liquidity gates to each preset.

    Event-risk and circuit-status checks require point-in-time external data
    that is not stored alongside the historical candle series. They remain
    available as standalone live filters, but must not silently pass inside a
    historical backtest.
    """
    return [
        _condition("data_completeness", {"minimum_candles": minimum_candles}, timeframe=Timeframe.ONE_DAY),
        _condition("average_traded_value", {"period": 30, "minimum": traded_value}, timeframe=Timeframe.ONE_DAY),
    ]


def build_filter_presets() -> list[FilterPreset]:
    """Return stable presets; these are templates, not live trading signals."""
    return [
        FilterPreset(
            preset_id="swing_volume_profile_pullback",
            name="Bullish swing volume-profile pullback",
            description=(
                "Confirm two higher highs and higher lows, build an estimated 70% volume "
                "value area across the latest swing low-to-high leg, then enter after a "
                "bullish rejection at that area. The profile estimates volume-at-price from "
                "5-minute OHLCV ranges; it is not trade-level volume-at-price data."
            ),
            category="pullback",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="1–15 trading days",
            max_hold_bars=_bars_for_trading_days(15),
            default_stop_loss_pct=5,
            default_take_profit_pct=10,
            expression=_expression([
                *_shared_gates(60),
                _condition(
                    "bullish_swing_poc_pullback",
                    {
                        "lookback": 750,
                        "pivot_window": 5,
                        "value_area_percent": 70,
                        "zone_buffer_pct": 0.25,
                    },
                ),
            ]),
        ),
        FilterPreset(
            preset_id="momentum_breakout",
            name="Short-term momentum breakout",
            description="Daily trend context with a 5-minute RSI, range breakout, and volume-confirmed entry.",
            category="momentum",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="1–10 trading days",
            max_hold_bars=_bars_for_trading_days(10),
            default_stop_loss_pct=4,
            default_take_profit_pct=8,
            expression=_expression([
                _condition("price_above_sma", {"period": 20, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("rsi_range", {"period": 14, "minimum": 55, "maximum": 80}),
                _condition("breakout", {"lookback": 20, "direction": "bullish", "volume_multiple": 1.5}),
                _condition("relative_volume", {"period": 20, "multiple": 1.5}),
            ]),
        ),
        FilterPreset(
            preset_id="trend_pullback",
            name="Trend pullback continuation",
            description="Daily trend alignment with a 5-minute pullback momentum and volatility check.",
            category="pullback",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="3–15 trading days",
            max_hold_bars=_bars_for_trading_days(15),
            default_stop_loss_pct=5,
            default_take_profit_pct=10,
            expression=_expression([
                _condition("price_above_sma", {"period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("ema_relation", {"fast_period": 20, "slow_period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("rsi_range", {"period": 14, "minimum": 40, "maximum": 62}),
                _condition("atr_percent", {"period": 14, "minimum": 1, "maximum": 8}),
            ]),
        ),
        FilterPreset(
            preset_id="volatility_breakout",
            name="Volatility and volume breakout",
            description="Daily trend context with a 5-minute directional breakout, volatility, and participation trigger.",
            category="breakout",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="1–8 trading days",
            max_hold_bars=_bars_for_trading_days(8),
            default_stop_loss_pct=5,
            default_take_profit_pct=12,
            expression=_expression([
                _condition("price_above_sma", {"period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("breakout", {"lookback": 20, "direction": "bullish", "volume_multiple": 1.5}),
                _condition("relative_volume", {"period": 20, "multiple": 1.5}),
                _condition("atr_percent", {"period": 14, "minimum": 2, "maximum": 12}),
            ]),
        ),
        FilterPreset(
            preset_id="trend_continuation",
            name="Strong trend continuation",
            description="Daily long-term trend and strength with a 5-minute participation trigger.",
            category="trend",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="5–30 trading days",
            max_hold_bars=_bars_for_trading_days(30),
            default_stop_loss_pct=7,
            default_take_profit_pct=15,
            expression=_expression([
                _condition("price_above_sma", {"period": 200, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("ema_relation", {"fast_period": 20, "slow_period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("adx_threshold", {"period": 14, "minimum": 20}, timeframe=Timeframe.ONE_DAY),
                _condition("relative_volume", {"period": 20, "multiple": 1.2}),
            ]),
        ),
        FilterPreset(
            preset_id="liquid_swing_momentum",
            name="Liquid swing momentum",
            description="Daily liquidity and trend gates combined with a 5-minute momentum entry.",
            category="swing",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="2–15 trading days",
            max_hold_bars=_bars_for_trading_days(15),
            default_stop_loss_pct=4,
            default_take_profit_pct=8,
            expression=_expression([
                _condition("average_traded_value", {"period": 30, "minimum": 10_000_000}, timeframe=Timeframe.ONE_DAY),
                _condition("price_above_sma", {"period": 20, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("rsi_range", {"period": 14, "minimum": 50, "maximum": 75}),
                _condition("relative_volume", {"period": 20, "multiple": 1.3}),
            ]),
        ),
        FilterPreset(
            preset_id="minervini_trend_leader",
            name="Minervini-style trend leader (technical)",
            description=(
                "Technical proxy for the Trend Template: daily 50/150/200-day trend context, "
                "a 5-minute breakout and participation trigger, plus data/liquidity gates. "
                "SMA-line alignment, 52-week range, moving-average slope, and fundamentals "
                "are not available in the current engine."
            ),
            category="trend",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="10–60 trading days",
            max_hold_bars=_bars_for_trading_days(60),
            default_stop_loss_pct=7,
            default_take_profit_pct=15,
            expression=_expression([
                *_shared_gates(260),
                _condition("price_above_sma", {"period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("price_above_sma", {"period": 150, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("price_above_sma", {"period": 200, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("ema_relation", {"fast_period": 20, "slow_period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("adx_threshold", {"period": 14, "minimum": 20}, timeframe=Timeframe.ONE_DAY),
                _condition("breakout", {"lookback": 20, "direction": "bullish", "volume_multiple": 1.5}),
                _condition("relative_volume", {"period": 20, "multiple": 1.5}),
                _condition("atr_percent", {"period": 14, "minimum": 1, "maximum": 12}),
            ]),
        ),
        FilterPreset(
            preset_id="oneil_growth_breakout",
            name="O'Neil growth breakout (technical)",
            description=(
                "Technical CAN SLIM/O'Neil approximation: daily trend context with a "
                "5-minute breakout, trend strength, and participation trigger. EPS, sales, ROE, "
                "industry rank, and fund ownership require a future fundamentals layer."
            ),
            category="breakout",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="5–63 trading days",
            max_hold_bars=_bars_for_trading_days(63),
            default_stop_loss_pct=7,
            default_take_profit_pct=15,
            expression=_expression([
                *_shared_gates(260),
                _condition("price_above_sma", {"period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("ema_relation", {"fast_period": 20, "slow_period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("rsi_range", {"period": 14, "minimum": 55, "maximum": 85}),
                _condition("adx_threshold", {"period": 14, "minimum": 20}, timeframe=Timeframe.ONE_DAY),
                _condition("breakout", {"lookback": 20, "direction": "bullish", "volume_multiple": 1.5}),
                _condition("relative_volume", {"period": 20, "multiple": 1.5}),
                _condition("atr_percent", {"period": 14, "minimum": 1, "maximum": 12}),
            ]),
        ),
        FilterPreset(
            preset_id="weinstein_stage2_approx",
            name="Weinstein Stage 2 (daily approximation)",
            description=(
                "Daily approximation of Stage 2 with a 5-minute participation trigger: "
                "long-term trend, directional strength, and range structure. Weekly 30-week "
                "slope and explicit stage classification are not available."
            ),
            category="trend",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="20–90 trading days",
            max_hold_bars=_bars_for_trading_days(90),
            default_stop_loss_pct=10,
            default_take_profit_pct=25,
            expression=_expression([
                *_shared_gates(180),
                _condition("price_above_sma", {"period": 150, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("ema_relation", {"fast_period": 30, "slow_period": 60, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("adx_threshold", {"period": 14, "minimum": 20}, timeframe=Timeframe.ONE_DAY),
                _condition("breakout", {"lookback": 52, "direction": "bullish", "volume_multiple": 2}, timeframe=Timeframe.ONE_DAY),
                _condition("relative_volume", {"period": 20, "multiple": 2}),
                _condition("atr_percent", {"period": 14, "minimum": 1, "maximum": 15}),
            ]),
        ),
        FilterPreset(
            preset_id="connors_rsi2_pullback",
            name="Connors RSI(2) pullback",
            description=(
                "5-minute RSI(2) mean-reversion entry aligned with the daily SMA(200) trend: "
                "close below the 5-minute SMA(5), liquid and tradable. Exit behavior remains "
                "controlled by the backtest risk defaults."
            ),
            category="mean_reversion",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="1–5 trading days",
            max_hold_bars=_bars_for_trading_days(5),
            default_stop_loss_pct=4,
            default_take_profit_pct=6,
            expression=_expression([
                *_shared_gates(210),
                _condition("price_above_sma", {"period": 200, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("price_above_sma", {"period": 5, "relation": "below"}),
                _condition("rsi_range", {"period": 2, "minimum": 0, "maximum": 10}),
            ]),
        ),
        FilterPreset(
            preset_id="ema_crossback_pullback",
            name="EMA crossback pullback (technical proxy)",
            description=(
                "Technical proxy for a 5-minute 10/20/50 EMA crossback aligned with the "
                "daily trend: moderate momentum, acceptable volatility, and support "
                "distance. The actual first retest shape and declining pullback volume "
                "are not directly available."
            ),
            category="pullback",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="3–15 trading days",
            max_hold_bars=_bars_for_trading_days(15),
            default_stop_loss_pct=5,
            default_take_profit_pct=10,
            expression=_expression([
                *_shared_gates(60),
                _condition("price_above_sma", {"period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("ema_relation", {"fast_period": 10, "slow_period": 20, "relation": "above"}),
                _condition("ema_relation", {"fast_period": 20, "slow_period": 50, "relation": "above"}),
                _condition("rsi_range", {"period": 14, "minimum": 35, "maximum": 65}),
                _condition("support_resistance", {"level": "support", "minimum_distance_pct": 0.5}),
                _condition("atr_percent", {"period": 14, "minimum": 1, "maximum": 8}),
            ]),
        ),
        FilterPreset(
            preset_id="trend_strength_baseline",
            name="ADX relative-volume trend baseline",
            description=(
                "Auditable multi-timeframe baseline: daily liquidity and SMA(50) context, "
                "5-minute ADX, bullish MACD, relative volume, volatility, and data/liquidity gates. "
                "Benchmark relative strength must be supplied separately."
            ),
            category="trend",
            timeframe=Timeframe.FIVE_MINUTE,
            holding_period="5–30 trading days",
            max_hold_bars=_bars_for_trading_days(30),
            default_stop_loss_pct=7,
            default_take_profit_pct=15,
            expression=_expression([
                *_shared_gates(60),
                _condition("price_above_sma", {"period": 50, "relation": "above"}, timeframe=Timeframe.ONE_DAY),
                _condition("adx_threshold", {"period": 14, "minimum": 20}),
                _condition("macd_direction", {"direction": "bullish"}),
                _condition("relative_volume", {"period": 20, "multiple": 1.2}),
                _condition("atr_percent", {"period": 14, "minimum": 1, "maximum": 10}),
            ]),
        ),
    ]


def get_filter_preset(preset_id: str) -> FilterPreset:
    for preset in build_filter_presets():
        if preset.preset_id == preset_id:
            return preset
    raise KeyError(f"unknown filter preset: {preset_id}")
