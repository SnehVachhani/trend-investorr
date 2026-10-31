package com.trendinvestorr.core.exceptions;

/**
 * Failed to generate a TOTP token. Port of {@code core.exceptions.TOTPGenerationError}.
 */
public class TOTPGenerationError extends AuthenticationError {
    public TOTPGenerationError(String message) {
        super(message);
    }
    public TOTPGenerationError(String message, Throwable cause) {
        super(message, cause);
    }
}
