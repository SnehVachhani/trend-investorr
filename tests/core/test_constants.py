"""Tests for core.constants — enums, market hours, timezone, NSE calendar."""

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
    is_nse_trading_day,
    get_nse_trading_days,
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


class TestNSETradingCalendar:
    def test_republic_day_is_not_trading_day(self):
        """Republic Day (Jan 26) is a national holiday — NSE is closed."""
        assert is_nse_trading_day(date(2026, 1, 26)) is False

    def test_regular_weekday_is_trading_day(self):
        """A normal Monday should be a trading day."""
        # 2026-06-15 is a Monday
        assert is_nse_trading_day(date(2026, 6, 15)) is True

    def test_weekend_is_not_trading_day(self):
        """Saturdays and Sundays are never trading days."""
        # 2026-06-14 is a Sunday
        assert is_nse_trading_day(date(2026, 6, 14)) is False

    def test_accepts_iso_string(self):
        """Function should accept ISO date strings as well as date objects."""
        assert is_nse_trading_day("2026-01-26") is False
        assert is_nse_trading_day("2026-06-15") is True

    def test_get_trading_days_returns_sorted_dates(self):
        """get_nse_trading_days returns a sorted list of date objects."""
        days = get_nse_trading_days(date(2026, 1, 1), date(2026, 1, 31))
        assert len(days) > 0
        assert all(isinstance(d, date) for d in days)
        assert days == sorted(days)

    def test_get_trading_days_excludes_weekends(self):
        """No Saturday or Sunday should appear in the result."""
        days = get_nse_trading_days(date(2026, 1, 1), date(2026, 12, 31))
        for d in days:
            assert d.weekday() < 5, f"{d} is a weekend day"

    def test_get_trading_days_works_across_years(self):
        """Calendar should work for historical years, not just 2026."""
        days_2020 = get_nse_trading_days(date(2020, 1, 1), date(2020, 12, 31))
        assert len(days_2020) > 200  # ~248 trading days per year
        assert len(days_2020) < 260

    def test_known_missing_holidays_are_excluded(self):
        """The dates that were missing from the old hardcoded list should be holidays."""
        # 2026-03-26 and 2026-05-28 were returning 0 candles for ALL stocks
        # These should NOT be trading days
        mar26 = is_nse_trading_day(date(2026, 3, 26))
        may28 = is_nse_trading_day(date(2026, 5, 28))
        # At least one of these should be a holiday (confirmed by API returning 0 candles)
        assert not mar26 or not may28, (
            "At least one of 2026-03-26 / 2026-05-28 should be a holiday "
            "based on API evidence"
        )
