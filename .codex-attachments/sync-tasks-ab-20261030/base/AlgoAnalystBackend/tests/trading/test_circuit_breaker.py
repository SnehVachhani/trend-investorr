"""Tests for trading.circuit_breaker — state transitions and auto-triggers."""

import pytest

from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from core.exceptions import CircuitBreakerOpenError


@pytest.fixture
def default_cb():
    """Circuit breaker with default config (3% daily loss, 5 consecutive, etc.)."""
    return CircuitBreaker(
        config=CircuitBreakerConfig(),
        total_capital=1_00_00_000.0,  # 1 Crore
    )


@pytest.fixture
def strict_cb():
    """Strict circuit breaker (low thresholds for testing)."""
    return CircuitBreaker(
        config=CircuitBreakerConfig(
            max_daily_loss_pct=1.0,
            max_consecutive_losses=3,
            max_drawdown_from_peak_pct=2.0,
            max_error_rate_pct=40.0,
            min_orders_for_error_check=3,
            half_open_max_trades=1,
        ),
        total_capital=1_00_000.0,  # 1 Lakh for easy math
    )


class TestCircuitBreakerInitialState:
    def test_initial_state_is_closed(self, default_cb):
        assert default_cb.state == CircuitState.CLOSED
        assert default_cb.is_closed is True
        assert default_cb.is_open is False

    def test_check_when_closed_no_exception(self, default_cb):
        default_cb.check()  # Should not raise

    def test_position_size_multiplier_closed(self, default_cb):
        assert default_cb.get_position_size_multiplier() == 1.0


class TestDailyLossTrigger:
    def test_trips_on_daily_loss_exceeded(self, strict_cb):
        """1% of 1L = 1000. Loss > 1000 should trip."""
        strict_cb.record_trade_result(-600)
        assert strict_cb.is_closed  # Still under limit

        strict_cb.record_trade_result(-500)
        assert strict_cb.is_open  # -1100 > -1000 limit


class TestConsecutiveLossTrigger:
    def test_trips_on_consecutive_losses(self, strict_cb):
        strict_cb.record_trade_result(-10)
        strict_cb.record_trade_result(-10)
        assert strict_cb.is_closed

        strict_cb.record_trade_result(-10)
        assert strict_cb.is_open  # 3 consecutive losses

    def test_win_resets_consecutive_count(self, strict_cb):
        strict_cb.record_trade_result(-10)
        strict_cb.record_trade_result(-10)
        strict_cb.record_trade_result(100)  # Win resets counter
        strict_cb.record_trade_result(-10)
        strict_cb.record_trade_result(-10)
        assert strict_cb.is_closed  # Only 2 consecutive after the win


class TestDrawdownTrigger:
    def test_trips_on_drawdown_from_peak(self, strict_cb):
        """2% drawdown from 1L peak = 2000."""
        strict_cb.record_trade_result(5000)  # Peak = 105000
        assert strict_cb.is_closed

        strict_cb.record_trade_result(-2200)  # Now at 102800, drawdown = 2200/105000 > 2%
        assert strict_cb.is_open


class TestErrorRateTrigger:
    def test_trips_on_high_error_rate(self, strict_cb):
        """40% error rate with min 3 orders."""
        strict_cb.record_order_failure()
        strict_cb.record_order_failure()
        assert strict_cb.is_closed  # Only 2 orders, below min_orders_for_error_check=3

        strict_cb.record_order_failure()  # 3/3 = 100% > 40%
        assert strict_cb.is_open


class TestCheckRaises:
    def test_check_when_open_raises(self, default_cb):
        default_cb.trip("test halt")
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            default_cb.check()
        assert "test halt" in str(exc_info.value)


class TestManualTrip:
    def test_manual_trip_sets_state(self, default_cb):
        default_cb.trip("admin override")
        assert default_cb.is_open
        assert default_cb.stats["trip_reason"] == "admin override"

    def test_trip_when_already_open_is_noop(self, default_cb):
        default_cb.trip("first trip")
        default_cb.trip("second trip")
        assert default_cb.stats["trip_count"] == 1
        assert default_cb.stats["trip_reason"] == "first trip"


class TestHalfOpen:
    def test_half_open_multiplier(self, default_cb):
        default_cb.trip("test")
        default_cb.half_open()
        assert default_cb.is_half_open
        assert default_cb.get_position_size_multiplier() == 0.5

    def test_half_open_recovery_on_win(self, strict_cb):
        strict_cb.trip("test")
        strict_cb.half_open()

        strict_cb.record_trade_result(100)  # Win in half-open
        # With half_open_max_trades=1, should recover after 1 winning trade
        assert strict_cb.is_closed

    def test_half_open_failure_on_loss(self, strict_cb):
        strict_cb.trip("test")
        strict_cb.half_open()

        strict_cb.record_trade_result(-100)  # Loss in half-open
        assert strict_cb.is_open  # Back to OPEN


class TestDailyReset:
    def test_auto_reset_on_new_day(self, default_cb):
        default_cb.trip("daily loss")
        assert default_cb.is_open

        default_cb.reset_daily()
        assert default_cb.is_closed

    def test_counters_reset(self, default_cb):
        default_cb.record_trade_result(-100)
        default_cb.record_trade_result(-100)
        default_cb.record_order_failure()

        default_cb.reset_daily()
        stats = default_cb.stats
        assert stats["consecutive_losses"] == 0
        assert stats["total_orders"] == 0
        assert stats["failed_orders"] == 0
