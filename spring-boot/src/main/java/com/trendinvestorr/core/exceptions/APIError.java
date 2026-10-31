package com.trendinvestorr.core.exceptions;

import java.util.HashMap;
import java.util.Map;

/**
 * Generic broker API error with HTTP status code.
 * Port of {@code core.exceptions.APIError}.
 */
public class APIError extends TrendInvestorrError {

    private final Integer statusCode;

    public APIError(String message, Integer statusCode) {
        super(message, withStatusCode(message, statusCode));
        this.statusCode = statusCode;
    }

    public APIError(String message) {
        this(message, null);
    }

    public Integer statusCode() {
        return statusCode;
    }

    private static Map<String, Object> withStatusCode(String message, Integer code) {
        Map<String, Object> d = new HashMap<>();
        d.put("status_code", code);
        d.put("message", message);
        return d;
    }
}
