package com.trendinvestorr.core.exceptions;

/**
 * Error with the encrypted credential vault (corrupt, wrong key, etc.).
 * Port of {@code core.exceptions.VaultError}.
 */
public class VaultError extends TrendInvestorrError {
    public VaultError(String message) { super(message); }
    public VaultError(String message, Throwable cause) { super(message, cause); }
}
