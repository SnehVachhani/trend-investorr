package com.trendinvestorr.data.model;

import java.time.Instant;

/**
 * Real-time price update from the WebSocket feed. Received at high frequency
 * during market hours. Port of {@code data.models.market_data.Tick}.
 */
public record Tick(
        String symbol,
        String token,
        double ltp,                // Last Traded Price
        Instant timestamp,
        long volume,
        double open,
        double high,
        double low,
        double close,              // previous day close
        double changePct,
        double bestBid,
        double bestAsk,
        long totalBuyQty,
        long totalSellQty,
        double avgTradePrice       // VWAP-like cumulative average
) {
    /** Bid-ask spread (0.0 when no bid). */
    public double spread() {
        if (bestBid == 0) return 0.0;
        return bestAsk - bestBid;
    }
}
