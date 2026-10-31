package com.trendinvestorr.core.exceptions;

import java.util.HashMap;
import java.util.Map;

/**
 * Not enough margin/funds to place the order.
 * Port of {@code core.exceptions.InsufficientFundsError}.
 */
public class InsufficientFundsError extends TrendInvestorrError {

    private final double required;
    private final double available;

    public InsufficientFundsError(double required, double available) {
        super(String.format(
                "Insufficient funds: required ₹%,.2f, available ₹%,.2f",
                required, available));
        this.required = required;
        this.available = available;
    }

    public double required() { return required; }
    public double available() { return available; }
}
