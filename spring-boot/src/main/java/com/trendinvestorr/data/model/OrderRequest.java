package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.core.constants.OrderType;
import com.trendinvestorr.core.constants.OrderVariety;
import com.trendinvestorr.core.constants.ProductType;
import com.trendinvestorr.core.constants.TransactionType;

/**
 * Order to be placed — validated by the RiskManager before execution.
 * Port of {@code data.models.market_data.OrderRequest}.
 */
public record OrderRequest(
        String symbol,
        String token,
        TransactionType transactionType,
        int quantity,
        OrderType orderType,
        ProductType productType,
        OrderVariety variety,
        double price,
        double triggerPrice,
        Exchange exchange,
        Long signalId            // link to originating signal
) {
    public OrderRequest {
        if (orderType == null) orderType = OrderType.MARKET;
        if (productType == null) productType = ProductType.INTRADAY;
        if (variety == null) variety = OrderVariety.NORMAL;
        if (exchange == null) exchange = Exchange.NSE;
    }

    public String tradingSymbol() {
        return symbol + "-EQ";
    }

    /** Estimated ₹ value (0 for market orders without a price). */
    public double estimatedValue() {
        return price > 0 ? price * quantity : 0.0;
    }
}
