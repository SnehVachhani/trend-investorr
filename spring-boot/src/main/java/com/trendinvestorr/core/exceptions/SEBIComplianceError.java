package com.trendinvestorr.core.exceptions;

/**
 * Action would violate SEBI algo trading regulations.
 * Port of {@code core.exceptions.SEBIComplianceError}.
 */
public class SEBIComplianceError extends TrendInvestorrError {
    public SEBIComplianceError(String message) { super(message); }
}
