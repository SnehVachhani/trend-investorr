package com.trendinvestorr.core.exceptions;

/**
 * Trade would exceed configured risk limits (exposure, position size, etc.).
 * Port of {@code core.exceptions.RiskLimitExceededError}.
 */
public class RiskLimitExceededError extends TrendInvestorrError {
    public RiskLimitExceededError(String message) {
        super(message);
    }
}
