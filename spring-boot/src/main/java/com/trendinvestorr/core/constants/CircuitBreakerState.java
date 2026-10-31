package com.trendinvestorr.core.constants;

/**
 * Circuit-breaker state machine. Port of {@code core.constants.CircuitBreakerState}.
 */
public enum CircuitBreakerState {
    CLOSED,         // Normal operation
    OPEN,           // Trading halted
    HALF_OPEN      // Testing with 1 trade
}
