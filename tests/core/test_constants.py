"""Tests for core.constants — enums, market hours, timezone."""

import pytest
from datetime import time, datetime, date

from core.constants import (
    IST,
    MARKET_OPEN,
    MARKET_CLOSE,
    Exchange,
    OrderType,
    TransactionType,
    ProductType,
    OrderStatus,
    SignalType,
    SignalStrength,
    AnalysisAction,
    CompositeStrategy,
    FeedMode,
    CandleInterval,
    AuditEventType,
    NSE_HOLIDAYS_2026,
)


class TestTimezone:
    def test_ist_offset_is_530(self):
        """IST timezone is UTC+05:30."""
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=IST)
        offset = dt.utcoffset()
        assert offset is not None
        # 5 hours 30 minutes = 19800 seconds
        assert offset.total_seconds() == 19800


class TestMarketHours:
    def test_market_open_is_915(self):
        assert MARKET_OPEN == time(9, 15)

    def test_market_close_is_1530(self):
        assert MARKET_CLOSE == time(15, 30)


class TestEnums:
    def test_exchange_members(self):
        assert Exchange.NSE.value == "NSE"
        assert Exchange.BSE.value == "BSE"
        assert Exchange.NFO.value == "NFO"

    def test_signal_strength_has_all_levels(self):
        strengths = [s.value for s in SignalStrength]
        assert "WEAK" in strengths
        assert "MODERATE" in strengths
        assert "STRONG" in strengths
        assert "EXTREME" in strengths

    def test_composite_strategy_has_required(self):
        strategies = [s.value for s in CompositeStrategy]
        assert "weighted" in strategies
        assert "unanimous" in strategies
        assert "majority" in strategies

    def test_all_analysis_actions_exist(self):
        actions = {a.value for a in AnalysisAction}
        assert actions == {"BUY", "SELL", "SKIP", "HOLD"}


class TestNSEHolidays:
    def test_holidays_are_parseable_dates(self):
        for date_str in NSE_HOLIDAYS_2026:
            parsed = date.fromisoformat(date_str)
            assert parsed.year == 2026, f"Holiday {date_str} is not in 2026"

    def test_republic_day_present(self):
        assert "2026-01-26" in NSE_HOLIDAYS_2026
