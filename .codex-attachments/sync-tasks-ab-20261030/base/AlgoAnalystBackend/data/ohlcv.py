"""Validation helpers for persisted market OHLCV bars."""

from __future__ import annotations

from math import isfinite
from typing import Any


def is_valid_ohlcv(open_: Any, high: Any, low: Any, close: Any, volume: Any) -> bool:
    """Return whether OHLCV values describe a usable positive-price candle."""
    try:
        open_value, high_value, low_value, close_value, volume_value = map(
            float, (open_, high, low, close, volume)
        )
    except (TypeError, ValueError, OverflowError):
        return False

    return (
        all(isfinite(value) for value in (open_value, high_value, low_value, close_value, volume_value))
        and low_value > 0
        and volume_value >= 0
        and high_value >= max(open_value, close_value, low_value)
        and low_value <= min(open_value, close_value)
    )
