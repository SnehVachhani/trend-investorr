package com.trendinvestorr.core.constants;

/**
 * Voting strategy used by the CompositeAnalyzer. Port of {@code core.constants.CompositeStrategy}.
 */
public enum CompositeStrategy {
    WEIGHTED,      // Weighted average of confidence scores
    UNANIMOUS,     // ALL analyzers must agree
    MAJORITY,      // >50% must agree
    PRIMARY        // First analyzer decides, others enrich
}
