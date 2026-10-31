package com.trendinvestorr.core.exceptions;

/**
 * Database connection or query error.
 * Port of {@code core.exceptions.DatabaseError}.
 */
public class DatabaseError extends TrendInvestorrError {
    public DatabaseError(String message) { super(message); }
    public DatabaseError(String message, Throwable cause) { super(message, cause); }
}
