"""Pure normalization helpers for Upstox fundamentals payloads."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime
from typing import Any


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "null", "-", "--"}:
        return None
    text = text.replace("%", "").replace("₹", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_period(value: Any) -> date | None:
    """Convert Upstox period labels and event dates into calendar dates."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            continue
    for fmt in ("%b %Y", "%B %Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            last_day = calendar.monthrange(parsed.year, parsed.month)[1]
            return date(parsed.year, parsed.month, last_day)
        except ValueError:
            continue
    return None


def normalize_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def extract_event_detail(event: dict[str, Any], *names: str) -> str | None:
    wanted = {normalize_label(name) for name in names}
    for detail in event.get("event_details", []) or []:
        if not isinstance(detail, dict):
            continue
        if normalize_label(detail.get("name")) in wanted:
            value = detail.get("value")
            return str(value).strip() if value is not None else None
    return None
