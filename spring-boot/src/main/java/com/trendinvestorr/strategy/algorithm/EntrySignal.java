package com.trendinvestorr.strategy.algorithm;

import com.trendinvestorr.data.model.Observation;

import java.util.List;
import java.util.Map;

/**
 * An algorithm's decision to enter a trade. NOT a scanner observation — a concrete
 * trading decision with specific price, stop loss, target, and position sizing intent.
 * Port of {@code strategies.base.EntrySignal}.
 */
public record EntrySignal(
        String symbol,
        String token,
        String algorithmName,
        String direction,                  // "BUY" or "SELL"
        double price,                       // intended entry price
        double stopLoss,
        double targetPrice,
        double confidence,                  // 0.0-1.0
        String reason,
        List<Observation> triggeringObservations,
        Map<String, Object> metadata
) {
    public EntrySignal {
        if (triggeringObservations == null) triggeringObservations = List.of();
        if (metadata == null) metadata = Map.of();
        if (direction == null) direction = "BUY";
        if (reason == null) reason = "";
    }
}
