package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.OrderStatus;

import java.time.Instant;

/**
 * Result of an order placement — from broker or paper trader.
 * Port of {@code data.models.market_data.OrderResult}.
 */
public record OrderResult(
        boolean success,
        String orderId,           // internal order ID
        String brokerOrderId,     // broker's order ID
        OrderStatus status,
        int filledQty,
        double avgFillPrice,
        String message,
        boolean isPaper,
        Instant timestamp
) {
    public OrderResult {
        if (orderId == null) orderId = "";
        if (brokerOrderId == null) brokerOrderId = "";
        if (status == null) status = OrderStatus.CREATED;
        if (message == null) message = "";
        if (timestamp == null) timestamp = Instant.now();
    }

    public OrderResult(boolean success, String orderId, OrderStatus status,
                       int filledQty, double avgFillPrice, String message, boolean isPaper) {
        this(success, orderId, "", status, filledQty, avgFillPrice, message, isPaper, Instant.now());
    }
}
