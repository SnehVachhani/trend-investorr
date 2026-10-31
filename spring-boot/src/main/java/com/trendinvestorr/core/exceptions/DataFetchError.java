package com.trendinvestorr.core.exceptions;

/**
 * Failed to fetch market data (bhavcopy, historical candles, etc.).
 * Port of {@code core.exceptions.DataFetchError}.
 */
public class DataFetchError extends TrendInvestorrError {
    public DataFetchError(String message) { super(message); }
    public DataFetchError(String message, Throwable cause) { super(message, cause); }
}
