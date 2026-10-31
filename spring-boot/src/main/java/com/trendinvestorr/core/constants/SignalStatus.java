package com.trendinvestorr.core.constants;

/**
 * Lifecycle of a generated signal. Port of {@code core.constants.SignalStatus}.
 */
public enum SignalStatus {
    PENDING,    // Generated, not yet acted upon
    ACTED,       // Order placed based on this signal
    EXPIRED,    // Cooldown passed without action
    SKIPPED       // Analyzer rejected the signal
}
