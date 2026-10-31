"""Normalize candle timestamps and keep replay data inside NSE sessions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from core.constants import IST, MARKET_CLOSE, MARKET_OPEN


INTRADAY_TIMEFRAMES = frozenset({"1m", "5m", "15m", "1h"})


def as_ist_datetime(value: Any, *, date_time: time | None = None) -> datetime | None:
    """Interpret a candle timestamp in exchange time, treating naive values as IST."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)
    if isinstance(value, date):
        if date_time is None:
            return None
        return datetime.combine(value, date_time, tzinfo=IST)
    if value is None:
        return None

    raw = str(value).strip()
    if len(raw) == 10:
        if date_time is None:
            return None
        try:
            return datetime.combine(date.fromisoformat(raw), date_time, tzinfo=IST)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def is_regular_session_candle(timestamp: Any) -> bool:
    """Return whether an intraday candle timestamp falls in the normal NSE session."""
    parsed = as_ist_datetime(timestamp)
    return parsed is not None and MARKET_OPEN <= parsed.time().replace(tzinfo=None) <= MARKET_CLOSE


def _row_value(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _signature(row: Any) -> tuple[Decimal | None, ...]:
    values: list[Decimal | None] = []
    for name in ("open", "high", "low", "close", "volume"):
        value = _row_value(row, name)
        if value is None:
            values.append(None)
            continue
        try:
            values.append(Decimal(str(value)).normalize())
        except (InvalidOperation, ValueError):
            values.append(None)
    return tuple(values)


def _with_timestamp(row: Any, timestamp: datetime) -> Any:
    if isinstance(row, dict):
        result = dict(row)
        result["timestamp"] = timestamp
        return result
    if hasattr(row, "model_copy"):
        return row.model_copy(update={"timestamp": timestamp})
    if hasattr(row, "_replace"):
        return row._replace(timestamp=timestamp)
    if hasattr(row, "__dataclass_fields__"):
        return replace(row, timestamp=timestamp)
    raise TypeError(f"Unsupported candle row type: {type(row).__name__}")


def normalize_daily_candles(rows: Iterable[Any]) -> tuple[list[Any], dict[str, int]]:
    """Return at most one daily candle per IST date, timestamped at the close.

    The source field is absent from the current daily table. When same-session
    rows conflict, an explicit 15:30 IST record wins because the application’s
    EOD aggregate and bhavcopy writers use that timestamp. If there is no unique
    close-stamped row, the conflicting session is omitted instead of guessing.
    """
    grouped: dict[tuple[Any, date], list[tuple[datetime, Any]]] = defaultdict(list)
    for row in rows:
        timestamp = as_ist_datetime(_row_value(row, "timestamp"), date_time=MARKET_CLOSE)
        if timestamp is not None:
            grouped[(_row_value(row, "stock_id"), timestamp.date())].append((timestamp, row))

    normalized: list[Any] = []
    duplicate_sessions = 0
    conflicting_sessions = 0
    skipped_ambiguous_sessions = 0

    for stock_id, session_date in sorted(grouped, key=lambda key: (str(key[0]), key[1])):
        session_rows = sorted(grouped[(stock_id, session_date)], key=lambda item: item[0])
        if len(session_rows) > 1:
            duplicate_sessions += 1

        signatures = {_signature(row) for _, row in session_rows}
        if len(signatures) == 1:
            selected = next(
                (row for timestamp, row in session_rows if timestamp.time().replace(tzinfo=None) == MARKET_CLOSE),
                session_rows[0][1],
            )
        else:
            conflicting_sessions += 1
            close_rows = [
                row for timestamp, row in session_rows
                if timestamp.time().replace(tzinfo=None) == MARKET_CLOSE
            ]
            if len(close_rows) == 1:
                selected = close_rows[0]
            else:
                skipped_ambiguous_sessions += 1
                continue

        close_timestamp = datetime.combine(session_date, MARKET_CLOSE, tzinfo=IST)
        normalized.append(_with_timestamp(selected, close_timestamp))

    return normalized, {
        "duplicate_sessions": duplicate_sessions,
        "conflicting_sessions": conflicting_sessions,
        "skipped_ambiguous_sessions": skipped_ambiguous_sessions,
    }
