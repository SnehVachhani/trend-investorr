package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.Exchange;

/**
 * An instrument in the tradable universe. Port of {@code data.models.market_data.Stock}.
 *
 * @param symbol       NSE symbol, e.g. "RELIANCE"
 * @param token        SmartAPI numeric token, e.g. "2885"
 * @param name         full company name
 * @param exchange     NSE / BSE / NFO / MCX
 * @param series       EQ, BE, etc.
 * @param sector       mapped sector name (from sectors.json)
 * @param industry     NSE industry classification
 * @param marketCapCr  market cap in crores
 * @param lotSize      F&O lot size (1 for equity)
 */
public record Stock(
        String symbol,
        String token,
        String name,
        Exchange exchange,
        String series,
        String sector,
        String industry,
        double marketCapCr,
        int lotSize
) {
    public Stock {
        if (exchange == null) exchange = Exchange.NSE;
        if (series == null || series.isBlank()) series = "EQ";
        if (sector == null) sector = "";
        if (industry == null) industry = "";
        if (marketCapCr < 0) marketCapCr = 0.0;
        if (lotSize <= 0) lotSize = 1;
    }

    /** SmartAPI trading-symbol format, e.g. {@code "RELIANCE-EQ"}. */
    public String tradingSymbol() {
        return symbol + "-" + series;
    }

    @Override
    public String toString() {
        return symbol + " (" + exchange + ")";
    }
}
