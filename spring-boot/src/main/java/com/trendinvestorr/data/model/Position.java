package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.constants.ProductType;

import java.time.Instant;

/**
 * A current open position (live or paper).
 * Port of {@code data.models.market_data.Position}.
 */
public record Position(
        String symbol,
        String token,
        int quantity,
        double avgBuyPrice,
        double currentPrice,
        ProductType productType,
        boolean isPaper,
        Instant openedAt
) {
    public Position {
        if (productType == null) productType = ProductType.INTRADAY;
        if (openedAt == null) {
            openedAt = Instant.now().atZone(MarketConstants.IST).toInstant();
        }
    }

    public double unrealizedPnl() {
        return (currentPrice - avgBuyPrice) * quantity;
    }

    public double unrealizedPnlPct() {
        if (avgBuyPrice == 0) return 0.0;
        return ((currentPrice - avgBuyPrice) / avgBuyPrice) * 100.0;
    }

    public double currentValue() {
        return currentPrice * quantity;
    }

    public double investedValue() {
        return avgBuyPrice * quantity;
    }
}
