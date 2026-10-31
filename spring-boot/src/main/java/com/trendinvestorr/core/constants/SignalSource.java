package com.trendinvestorr.core.constants;

/**
 * Identifies which scanner originated a signal, used for performance attribution
 * and confluence scoring in the AlgorithmManager.
 * Port of {@code core.constants.SignalSource}.
 */
public enum SignalSource {
    VOLUME_SPIKE("volume_spike"),
    MOMENTUM("momentum"),
    BREAKOUT("breakout"),
    ORB("orb"),
    DELIVERY("delivery"),
    VWAP("vwap"),
    GAP("gap"),
    RELATIVE_STRENGTH("relative_strength"),
    SECTOR_ROTATION("sector_rotation"),
    ORDER_FLOW("order_flow"),
    PATTERN("pattern"),
    MTF_CONFLUENCE("mtf_confluence"),
    LIQUIDITY_SWEEP("liquidity_sweep");

    private final String code;

    SignalSource(String code) {
        this.code = code;
    }

    public String code() {
        return code;
    }

    public static SignalSource fromCode(String code) {
        if (code == null) {
            return PATTERN;
        }
        for (SignalSource s : values()) {
            if (s.code.equalsIgnoreCase(code)) {
                return s;
            }
        }
        return PATTERN;
    }
}
