package com.trendinvestorr.core.exceptions;

/**
 * Today is an NSE holiday — no trading.
 * Port of {@code core.exceptions.MarketHolidayError}.
 */
public class MarketHolidayError extends MarketClosedError {
    public MarketHolidayError(String message) {
        super(message);
    }
}
