package com.trendinvestorr.core.exceptions;

/**
 * Circuit breaker is OPEN — all trading is halted.
 * Port of {@code core.exceptions.CircuitBreakerOpenError}.
 */
public class CircuitBreakerOpenError extends TrendInvestorrError {

    private final String reason;
    private final int cooldownMinutes;

    public CircuitBreakerOpenError(String reason, int cooldownMinutes) {
        super(String.format(
                "Circuit breaker OPEN: %s. Cooldown: %d min",
                reason, cooldownMinutes));
        this.reason = reason;
        this.cooldownMinutes = cooldownMinutes;
    }

    public String reason() { return reason; }
    public int cooldownMinutes() { return cooldownMinutes; }
}
