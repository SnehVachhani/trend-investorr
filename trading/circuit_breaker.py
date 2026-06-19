"""
Trend-Investorr: Circuit Breaker

Automatic trading halt when loss or error thresholds are exceeded.
Similar to exchange-level circuit breakers but at the system level.

States:
    CLOSED  — Normal trading (default)
    OPEN    — Trading halted (breaker tripped)
    HALF    — Limited trading (recovery mode, reduced position sizes)

Triggers:
    1. Daily P&L loss exceeds threshold
    2. Consecutive losing trades exceed limit
    3. Error rate exceeds threshold (API failures, rejections)
    4. Manual trigger (admin override)

Recovery:
    - Auto-reset at start of next trading day
    - Manual reset via admin command
    - HALF_OPEN allows reduced trading to test recovery
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from loguru import logger

from core.constants import AuditEventType, IST
from core.exceptions import CircuitBreakerOpenError
from core.security.audit import AuditTrail


class CircuitState(str, Enum):
    CLOSED = "CLOSED"       # Normal trading
    OPEN = "OPEN"           # Trading halted
    HALF_OPEN = "HALF_OPEN" # Recovery mode — reduced sizing


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker thresholds — runtime-tunable."""

    # Loss triggers
    max_daily_loss_pct: float = 3.0        # Trip at 3% daily loss
    max_consecutive_losses: int = 5        # Trip after 5 consecutive losses
    max_drawdown_from_peak_pct: float = 5.0  # Trip at 5% drawdown from peak

    # Error triggers
    max_error_rate_pct: float = 50.0       # Trip if >50% of orders fail
    min_orders_for_error_check: int = 5    # Need at least 5 orders to check error rate

    # Recovery
    half_open_position_size_pct: float = 50.0  # 50% of normal size in half-open
    half_open_max_trades: int = 2              # Max trades in half-open before re-evaluating

    # Auto-reset
    auto_reset_next_day: bool = True       # Reset to CLOSED at start of new day


class CircuitBreaker:
    """
    System-level circuit breaker for automatic trading halts.

    Usage:
        cb = CircuitBreaker(config, audit)
        cb.check()                          # Raises if OPEN
        cb.record_trade_result(pnl=-500)   # Updates state
        cb.trip("Manual halt")             # Force trip
        cb.reset()                         # Manual reset
    """

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        audit: AuditTrail | None = None,
        total_capital: float = 1_00_00_000.0,
    ):
        self._config = config or CircuitBreakerConfig()
        self._audit = audit
        self._total_capital = total_capital

        # State
        self._state = CircuitState.CLOSED
        self._tripped_at: datetime | None = None
        self._trip_reason: str = ""

        # Tracking
        self._daily_pnl: float = 0.0
        self._peak_capital: float = total_capital
        self._consecutive_losses: int = 0
        self._total_orders: int = 0
        self._failed_orders: int = 0
        self._half_open_trades: int = 0
        self._trip_count: int = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitState.CLOSED

    @property
    def is_half_open(self) -> bool:
        return self._state == CircuitState.HALF_OPEN

    def check(self) -> None:
        """
        Check if trading is allowed.

        Raises:
            CircuitBreakerOpenError: If the circuit breaker is OPEN.
        """
        if self._state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker OPEN — trading halted. "
                f"Reason: {self._trip_reason}. "
                f"Tripped at: {self._tripped_at}",
                details={
                    "state": self._state.value,
                    "reason": self._trip_reason,
                    "tripped_at": str(self._tripped_at),
                    "daily_pnl": self._daily_pnl,
                    "consecutive_losses": self._consecutive_losses,
                },
            )

    def get_position_size_multiplier(self) -> float:
        """
        Get position size multiplier based on circuit state.

        CLOSED:    1.0 (full size)
        HALF_OPEN: 0.5 (reduced)
        OPEN:      0.0 (no trading)
        """
        match self._state:
            case CircuitState.CLOSED:
                return 1.0
            case CircuitState.HALF_OPEN:
                return self._config.half_open_position_size_pct / 100.0
            case CircuitState.OPEN:
                return 0.0

    def record_trade_result(self, pnl: float) -> None:
        """
        Record a completed trade's P&L and check triggers.

        Args:
            pnl: Profit (positive) or loss (negative) from the trade.
        """
        self._daily_pnl += pnl
        self._total_orders += 1

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0  # Reset on win

        # Update peak capital
        current_capital = self._total_capital + self._daily_pnl
        if current_capital > self._peak_capital:
            self._peak_capital = current_capital

        # Check triggers
        self._check_daily_loss_trigger()
        self._check_consecutive_loss_trigger()
        self._check_drawdown_trigger()

        # Half-open tracking
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_trades += 1
            if self._half_open_trades >= self._config.half_open_max_trades:
                if pnl >= 0:
                    self._recover()
                else:
                    self.trip("Half-open recovery failed — re-halting")

    def record_order_failure(self) -> None:
        """Record a failed order (API error, rejection) and check error rate."""
        self._failed_orders += 1
        self._total_orders += 1
        self._check_error_rate_trigger()

    def trip(self, reason: str) -> None:
        """
        Manually trip the circuit breaker.

        Args:
            reason: Human-readable reason for the halt.
        """
        if self._state == CircuitState.OPEN:
            return  # Already open

        self._state = CircuitState.OPEN
        self._tripped_at = datetime.now(IST)
        self._trip_reason = reason
        self._trip_count += 1

        logger.warning(f"🔴 CIRCUIT BREAKER TRIPPED: {reason}")

        if self._audit:
            self._audit.log(
                AuditEventType.CIRCUIT_BREAKER_CHANGE,
                {
                    "new_state": CircuitState.OPEN.value,
                    "reason": reason,
                    "daily_pnl": self._daily_pnl,
                    "consecutive_losses": self._consecutive_losses,
                    "trip_count": self._trip_count,
                },
            )

    def half_open(self) -> None:
        """Transition to HALF_OPEN for recovery testing."""
        self._state = CircuitState.HALF_OPEN
        self._half_open_trades = 0

        logger.info("🟡 Circuit breaker → HALF_OPEN (recovery mode)")

        if self._audit:
            self._audit.log(
                AuditEventType.CIRCUIT_BREAKER_CHANGE,
                {"new_state": CircuitState.HALF_OPEN.value},
            )

    def reset(self, reason: str = "Manual reset") -> None:
        """Reset circuit breaker to CLOSED."""
        old_state = self._state
        self._state = CircuitState.CLOSED
        self._trip_reason = ""
        self._tripped_at = None
        self._half_open_trades = 0

        logger.info(f"🟢 Circuit breaker RESET ({old_state.value} → CLOSED): {reason}")

        if self._audit:
            self._audit.log(
                AuditEventType.CIRCUIT_BREAKER_CHANGE,
                {
                    "new_state": CircuitState.CLOSED.value,
                    "old_state": old_state.value,
                    "reason": reason,
                },
            )

    def reset_daily(self) -> None:
        """Reset for a new trading day."""
        if self._config.auto_reset_next_day and self._state == CircuitState.OPEN:
            self.reset("Auto-reset for new trading day")

        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._total_orders = 0
        self._failed_orders = 0
        self._half_open_trades = 0
        logger.info("Circuit breaker daily counters reset")

    # ── Trigger Checks ───────────────────────────────────

    def _check_daily_loss_trigger(self) -> None:
        max_loss = self._total_capital * (self._config.max_daily_loss_pct / 100)
        if self._daily_pnl < -max_loss:
            self.trip(
                f"Daily loss limit exceeded: ₹{self._daily_pnl:,.0f} "
                f"(limit: -₹{max_loss:,.0f})"
            )

    def _check_consecutive_loss_trigger(self) -> None:
        if self._consecutive_losses >= self._config.max_consecutive_losses:
            self.trip(
                f"Consecutive loss limit: {self._consecutive_losses} losses in a row "
                f"(limit: {self._config.max_consecutive_losses})"
            )

    def _check_drawdown_trigger(self) -> None:
        current = self._total_capital + self._daily_pnl
        if self._peak_capital > 0:
            drawdown_pct = ((self._peak_capital - current) / self._peak_capital) * 100
            if drawdown_pct >= self._config.max_drawdown_from_peak_pct:
                self.trip(
                    f"Drawdown limit: {drawdown_pct:.1f}% from peak "
                    f"(limit: {self._config.max_drawdown_from_peak_pct}%)"
                )

    def _check_error_rate_trigger(self) -> None:
        if self._total_orders >= self._config.min_orders_for_error_check:
            error_rate = (self._failed_orders / self._total_orders) * 100
            if error_rate >= self._config.max_error_rate_pct:
                self.trip(
                    f"Error rate too high: {error_rate:.0f}% "
                    f"({self._failed_orders}/{self._total_orders} failed)"
                )

    def _recover(self) -> None:
        """Recover from half-open to closed."""
        self.reset("Half-open recovery successful")

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "trip_reason": self._trip_reason,
            "tripped_at": str(self._tripped_at) if self._tripped_at else None,
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "total_orders": self._total_orders,
            "failed_orders": self._failed_orders,
            "trip_count": self._trip_count,
            "position_size_multiplier": self.get_position_size_multiplier(),
        }

    def update_config(self, config: CircuitBreakerConfig) -> None:
        self._config = config
