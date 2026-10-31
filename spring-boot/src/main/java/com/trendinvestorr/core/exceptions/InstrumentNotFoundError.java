package com.trendinvestorr.core.exceptions;

/**
 * Stock symbol or token not found in the instrument master list.
 * Port of {@code core.exceptions.InstrumentNotFoundError}.
 */
public class InstrumentNotFoundError extends TrendInvestorrError {

    private final String identifier;

    public InstrumentNotFoundError(String identifier) {
        super(String.format("Instrument not found: '%s'", identifier));
        this.identifier = identifier;
    }

    public String identifier() { return identifier; }
}
