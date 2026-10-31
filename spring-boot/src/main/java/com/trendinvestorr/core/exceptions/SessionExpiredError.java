package com.trendinvestorr.core.exceptions;

/**
 * Broker API session has expired and needs re-authentication.
 * Port of {@code core.exceptions.SessionExpiredError}.
 */
public class SessionExpiredError extends AuthenticationError {
    public SessionExpiredError(String message) {
        super(message);
    }
}
