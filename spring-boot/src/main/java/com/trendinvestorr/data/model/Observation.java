package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.constants.SignalSource;

import java.time.Instant;
import java.util.Map;

/**
 * A raw observation from a scanner. This is a FACT, not a trade decision.
 * Port of {@code data.models.market_data.Observation}.
 *
 * <p>Scanners detect patterns and emit observations. The AlgorithmManager collects these
 * and routes them to trading algorithms that decide whether to trade.
 *
 * <p>Example: "RELIANCE broke above resistance ₹2,850" is an observation.
 * "BUY RELIANCE" is a trade decision (made by an algorithm).
 */
public record Observation(
        String symbol,
        String token,
        SignalSource source,
        String direction,         // "bullish" or "bearish"
        double confidence,        // 0.0 - 1.0
        double price,
        String patternName,
        Instant timestamp,
        Map<String, Object> details
) {
    public Observation {
        if (timestamp == null) {
            timestamp = Instant.now().atZone(MarketConstants.IST).toInstant();
        }
        if (details == null) details = Map.of();
        if (patternName == null) patternName = "";
    }

    public Observation(String symbol, String token, SignalSource source, String direction,
                       double confidence, double price, String patternName,
                       Map<String, Object> details) {
        this(symbol, token, source, direction, confidence, price, patternName, null, details);
    }
}
