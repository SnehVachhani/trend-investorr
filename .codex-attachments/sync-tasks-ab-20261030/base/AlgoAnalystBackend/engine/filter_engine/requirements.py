"""History and live-data requirements for catalog filters."""

from __future__ import annotations

from typing import Any

from .models import DataRequirement, Timeframe


def _integer(params: dict[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def history_for(filter_id: str, params: dict[str, Any], timeframe: Timeframe) -> DataRequirement:
    """Resolve the minimum historical data for one parameterized filter."""
    period = _integer(params, "period", 14)
    lookback = _integer(params, "lookback", 20)
    mapping = {
        # The configured minimum is itself the completeness contract.  A
        # technical-indicator warm-up does not belong on this gate; adding it
        # made a request for exactly N candles incorrectly report insufficient
        # data when N + 5 candles were not present.
        "data_completeness": (30, 0, False, "timestamp", "OHLCV history must be complete"),
        "price_range": (1, 0, False, "close", "latest close is required"),
        "average_volume": (period, 5, False, "volume", "volume average"),
        "average_traded_value": (period, 5, False, "close,volume", "traded-value average"),
        "price_above_sma": (period, 5, False, "close", f"SMA({period})"),
        "ema_relation": (max(_integer(params, "fast_period", 20), _integer(params, "slow_period", 50)), 10, False, "close", "EMA relationship"),
        "rsi_range": (period + 1, 5, False, "close", f"RSI({period})"),
        "macd_direction": (35, 5, False, "close", "MACD(12,26,9)"),
        "atr_percent": (period + 1, 5, False, "high,low,close", f"ATR({period})"),
        "adx_threshold": (period * 2, 5, False, "high,low,close", f"ADX({period})"),
        "relative_volume": (period + 1, 5, False, "volume", "relative volume"),
        "price_above_vwap": (1, 0, True, "high,low,close,volume", "VWAP/live quote"),
        "previous_level": (2, 0, False, "high,low,close", "previous candle level"),
        "support_resistance": (20, 5, False, "high,low,close", "swing support/resistance"),
        "relative_strength": (period + 1, 5, False, "close", "stock and benchmark returns"),
        "breakout": (lookback + 1, 5, False, "high,low,close,volume", "breakout lookback"),
        "trendline_break": (2, 0, False, "close", "manual trendline points and latest close"),
        "event_risk": (0, 0, False, "events", "corporate-event flags"),
        "circuit_status": (0, 0, True, "quote", "current circuit/suspension status"),
    }
    minimum, warmup, live, fields, reason = mapping.get(
        filter_id,
        (1, 0, False, "close", "filter input"),
    )
    if filter_id == "bullish_swing_poc_pullback":
        pivot_window = _integer(params, "pivot_window", 3)
        minimum = _integer(params, "lookback", 250) + pivot_window * 2
        warmup = 0
        fields = "open,high,low,close,volume"
        reason = "confirmed swing structure and volume profile"
    return DataRequirement(
        interval=timeframe,
        minimum_candles=minimum,
        warmup=warmup,
        required_fields=fields.split(","),
        requires_live=live,
        reason=reason,
    )
