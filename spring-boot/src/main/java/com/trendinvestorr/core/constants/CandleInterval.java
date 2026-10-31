package com.trendinvestorr.core.constants;

import java.util.Map;

/**
 * Candle time-frame buckets. Port of {@code core.constants.CandleInterval}.
 *
 * <p>Each value carries the DB table suffix it maps to — e.g. {@link #ONE_DAY} → {@code candles_1d}.
 * The original Python code kept a separate {@code CANDLE_TABLE_SUFFIX} dict; here we embed the
 * suffix as a field on the enum so the mapping is local and compile-time.
 */
public enum CandleInterval {
    ONE_MINUTE("1m"),
    FIVE_MINUTE("5m"),
    FIFTEEN_MINUTE("15m"),
    ONE_HOUR("1h"),
    ONE_DAY("1d"),
    ONE_WEEK("1w");

    private final String suffix;

    CandleInterval(String suffix) {
        this.suffix = suffix;
    }

    /** DB table suffix, e.g. {@code "1d"} for {@code candles_1d}. */
    public String suffix() {
        return suffix;
    }

    /** Resolved DB table name, e.g. {@code "candles_1d"}. */
    public String tableName() {
        return "candles_" + suffix;
    }

    public static CandleInterval fromSuffix(String suffix) {
        for (CandleInterval c : values()) {
            if (c.suffix.equalsIgnoreCase(suffix)) {
                return c;
            }
        }
        throw new IllegalArgumentException("Unknown candle suffix: " + suffix);
    }
}
