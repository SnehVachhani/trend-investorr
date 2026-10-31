package com.trendinvestorr.core.exceptions;

/**
 * Failed to square off an intraday position before market close.
 * Port of {@code core.exceptions.PositionSquareOffError}.
 */
public class PositionSquareOffError extends TrendInvestorrError {
    public PositionSquareOffError(String message) {
        super(message);
    }
}
