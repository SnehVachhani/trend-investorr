package com.trendinvestorr.core.exceptions;

/**
 * Broker rejected an order. Port of {@code core.exceptions.OrderRejectedError}.
 */
public class OrderRejectedError extends TrendInvestorrError {

    private final String symbol;
    private final String reason;
    private final String brokerOrderId;

    public OrderRejectedError(String symbol, String reason, String brokerOrderId) {
        super(String.format("Order rejected for %s: %s", symbol, reason));
        this.symbol = symbol;
        this.reason = reason;
        this.brokerOrderId = brokerOrderId;
    }

    public OrderRejectedError(String symbol, String reason) {
        this(symbol, reason, null);
    }

    public String symbol() { return symbol; }
    public String reason() { return reason; }
    public String brokerOrderId() { return brokerOrderId; }
}
