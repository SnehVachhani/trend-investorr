package com.trendinvestorr.core.exceptions;

import java.util.Map;

/**
 * Base exception for all Trend-Investorr errors. Port of {@code core.exceptions.TrendInvestorrError}.
 *
 * <p>Allows catching every app error with a single {@code catch (TrendInvestorrError)}
 * clause while still supporting granular handling of specific subclasses.
 */
public class TrendInvestorrError extends RuntimeException {

    private final String detailMessage;
    private final Map<String, Object> details;

    public TrendInvestorrError(String message) {
        this(message, Map.of());
    }

    public TrendInvestorrError(String message, Map<String, Object> details) {
        super(message);
        this.detailMessage = message;
        this.details = details == null ? Map.of() : details;
    }

    public TrendInvestorrError(String message, Throwable cause) {
        super(message, cause);
        this.detailMessage = message;
        this.details = Map.of();
    }

    public Map<String, Object> details() {
        return details;
    }

    @Override
    public String toString() {
        if (details != null && !details.isEmpty()) {
            return detailMessage + " | Details: " + details;
        }
        return detailMessage;
    }
}
