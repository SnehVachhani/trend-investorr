package com.trendinvestorr.core.exceptions;

/**
 * Attempted to trade outside market hours.
 * Port of {@code core.exceptions.MarketClosedError}.
 */
public class MarketClosedError extends TrendInvestorrError {
    public MarketClosedError(String message) {
        super(message);
    }
}
