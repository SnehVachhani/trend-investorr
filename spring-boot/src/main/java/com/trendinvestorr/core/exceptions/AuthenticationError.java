package com.trendinvestorr.core.exceptions;

/**
 * Failed to authenticate with broker API (invalid credentials, TOTP expired, etc.).
 * Port of {@code core.exceptions.AuthenticationError}.
 */
public class AuthenticationError extends TrendInvestorrError {
    public AuthenticationError(String message) {
        super(message);
    }
    public AuthenticationError(String message, Throwable cause) {
        super(message, cause);
    }
}
