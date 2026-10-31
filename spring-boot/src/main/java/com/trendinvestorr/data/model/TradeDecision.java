package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.constants.SignalSource;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * A trade decision from the ScannerConfluenceAlgo. Created when multiple scanner
 * observations agree (confluence). This is the ONLY object that should trigger
 * order execution. Port of {@code data.models.market_data.TradeDecision}.
 */
public record TradeDecision(
        String symbol,
        String token,
        String direction,                  // "BUY" or "SELL"
        double confluenceScore,             // weighted score across all agreeing scanners
        List<SignalSource> contributingSources,
        List<Observation> observations,
        Instant timestamp,
        Map<String, Object> metadata
) {
    public TradeDecision {
        if (contributingSources == null) contributingSources = List.of();
        if (observations == null) observations = List.of();
        if (metadata == null) metadata = Map.of();
        if (timestamp == null) {
            timestamp = Instant.now().atZone(MarketConstants.IST).toInstant();
        }
    }

    /** Number of unique scanners that contributed. */
    public int scannerCount() {
        return (int) contributingSources.stream().distinct().count();
    }
}
