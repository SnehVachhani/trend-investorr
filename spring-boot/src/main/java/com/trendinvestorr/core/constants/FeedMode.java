package com.trendinvestorr.core.constants;

/**
 * SmartAPI WebSocket feed modes. Port of {@code core.constants.FeedMode}.
 */
public enum FeedMode {
    LTP(1),          // Last Traded Price only
    QUOTE(2),        // Full quote snapshot
    SNAP_QUOTE(3);   // OHLCV + Volume snapshot

    private final int code;

    FeedMode(int code) {
        this.code = code;
    }

    public int code() {
        return code;
    }
}
