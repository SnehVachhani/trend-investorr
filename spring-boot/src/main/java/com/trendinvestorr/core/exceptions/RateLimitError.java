package com.trendinvestorr.core.exceptions;

import java.util.HashMap;
import java.util.Map;

/**
 * API rate limit exceeded. Carries a {@code retryAfterSeconds} hint.
 * Port of {@code core.exceptions.RateLimitError}.
 */
public class RateLimitError extends TrendInvestorrError {

    private final double retryAfterSeconds;

    public RateLimitError(String message, double retryAfterSeconds) {
        super(message, withDetail(message, retryAfterSeconds));
        this.retryAfterSeconds = retryAfterSeconds;
    }

    public RateLimitError() {
        this("Rate limit exceeded", 1.0);
    }

    public double retryAfterSeconds() {
        return retryAfterSeconds;
    }

    private static Map<String, Object> withDetail(String message, double r) {
        Map<String, Object> d = new HashMap<>();
        d.put("retry_after_seconds", r);
        d.put("message", message);
        return d;
    }
}
