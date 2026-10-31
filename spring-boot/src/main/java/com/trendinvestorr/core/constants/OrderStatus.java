package com.trendinvestorr.core.constants;

/**
 * Lifecycle state of an order. Port of {@code core.constants.OrderStatus}.
 */
public enum OrderStatus {
    CREATED,             // Internal — before sending to broker
    PLACED,              // Sent to broker, pending execution
    FILLED,              // Fully executed
    PARTIALLY_FILLED,
    REJECTED,
    CANCELLED
}
