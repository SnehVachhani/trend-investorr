package com.trendinvestorr.core.constants;

/**
 * Signal strength bands based on volume multiples. Port of {@code core.constants.SignalStrength}.
 */
public enum SignalStrength {
    WEAK,         // Volume 1.5x-2x average
    MODERATE,     // Volume 2x-3x average
    STRONG,       // Volume 3x-5x average
    EXTREME       // Volume 5x+ average
}
