package com.trendinvestorr.core.exceptions;

/**
 * Invalid or missing configuration value.
 * Port of {@code core.exceptions.ConfigurationError}.
 */
public class ConfigurationError extends TrendInvestorrError {
    public ConfigurationError(String message) { super(message); }
    public ConfigurationError(String message, Throwable cause) { super(message, cause); }
}
