package com.trendinvestorr.strategy.algorithm;

import java.util.Map;

/**
 * An algorithm's decision to exit a trade. Port of {@code strategies.base.ExitSignal}.
 */
public record ExitSignal(
        ActiveTrade trade,
        ExitReason reason,
        double exitPrice,
        String description,
        Map<String, Object> metadata
) {
    public ExitSignal {
        if (metadata == null) metadata = Map.of();
        if (description == null) description = "";
    }
}
