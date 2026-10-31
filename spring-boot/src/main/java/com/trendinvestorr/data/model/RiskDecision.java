package com.trendinvestorr.data.model;

/**
 * Output from the RiskManager — approve or reject a trade.
 * Port of {@code data.models.market_data.RiskDecision}.
 */
public record RiskDecision(
        boolean approved,
        int quantity,             // approved quantity (may be less than requested)
        String reason,
        double maxPositionValue,
        double stopLossPrice,
        double targetPrice
) {
    public RiskDecision {
        if (reason == null) reason = "";
    }

    public static RiskDecision reject(String reason) {
        return new RiskDecision(false, 0, reason, 0.0, 0.0, 0.0);
    }

    public static RiskDecision approve(int qty, double maxVal, double sl, double target, String reason) {
        return new RiskDecision(true, qty, reason, maxVal, sl, target);
    }
}
