"""Tests for core.exceptions — exception hierarchy, details, formatting."""

import pytest
from core.exceptions import (
    TrendInvestorrError,
    AuthenticationError,
    SessionExpiredError,
    TOTPGenerationError,
    RateLimitError,
    APIError,
    InsufficientFundsError,
    OrderRejectedError,
    PositionSquareOffError,
    CircuitBreakerOpenError,
    RiskLimitExceededError,
    MarketClosedError,
    MarketHolidayError,
    DataFetchError,
    WebSocketError,
    InstrumentNotFoundError,
    VaultError,
    SEBIComplianceError,
    ConfigurationError,
    DatabaseError,
)


class TestTrendInvestorrError:
    """Base exception tests."""

    def test_stores_message_and_details(self):
        err = TrendInvestorrError("test error", details={"key": "value"})
        assert err.message == "test error"
        assert err.details == {"key": "value"}

    def test_str_includes_details_when_present(self):
        err = TrendInvestorrError("fail", details={"code": 500})
        text = str(err)
        assert "fail" in text
        assert "500" in text

    def test_str_omits_details_when_empty(self):
        err = TrendInvestorrError("simple error")
        text = str(err)
        assert text == "simple error"
        assert "Details" not in text

    def test_empty_details_defaults_to_dict(self):
        err = TrendInvestorrError("msg")
        assert err.details == {}


class TestExceptionHierarchy:
    """All exceptions inherit from TrendInvestorrError."""

    ALL_EXCEPTIONS = [
        AuthenticationError,
        SessionExpiredError,
        TOTPGenerationError,
        RateLimitError,
        APIError,
        InsufficientFundsError,
        OrderRejectedError,
        PositionSquareOffError,
        CircuitBreakerOpenError,
        RiskLimitExceededError,
        MarketClosedError,
        MarketHolidayError,
        DataFetchError,
        WebSocketError,
        InstrumentNotFoundError,
        VaultError,
        SEBIComplianceError,
        ConfigurationError,
        DatabaseError,
    ]

    def test_all_subclasses_inherit_from_base(self):
        for exc_class in self.ALL_EXCEPTIONS:
            assert issubclass(exc_class, TrendInvestorrError), (
                f"{exc_class.__name__} doesn't inherit from TrendInvestorrError"
            )


class TestSpecificExceptions:
    """Test exceptions with custom __init__ signatures."""

    def test_insufficient_funds_stores_amounts(self):
        err = InsufficientFundsError(required=50000, available=10000)
        assert err.required == 50000
        assert err.available == 10000
        assert "₹50,000" in str(err)
        assert "₹10,000" in str(err)

    def test_order_rejected_stores_fields(self):
        err = OrderRejectedError(symbol="RELIANCE", reason="margin", broker_order_id="12345")
        assert err.symbol == "RELIANCE"
        assert err.reason == "margin"
        assert err.broker_order_id == "12345"
        assert "RELIANCE" in str(err)

    def test_circuit_breaker_open_stores_cooldown(self):
        err = CircuitBreakerOpenError(reason="daily loss", cooldown_minutes=60)
        assert err.reason == "daily loss"
        assert err.cooldown_minutes == 60
        assert "daily loss" in str(err)

    def test_rate_limit_stores_retry_after(self):
        err = RateLimitError(retry_after=5.0)
        assert err.retry_after == 5.0
        assert err.details["retry_after_seconds"] == 5.0

    def test_instrument_not_found_stores_identifier(self):
        err = InstrumentNotFoundError("UNKNOWN_STOCK")
        assert err.identifier == "UNKNOWN_STOCK"
        assert "UNKNOWN_STOCK" in str(err)
