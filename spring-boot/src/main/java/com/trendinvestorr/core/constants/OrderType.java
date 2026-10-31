package com.trendinvestorr.core.constants;

/**
 * Order types supported by the SmartAPI broker. Port of {@code core.constants.OrderType}.
 */
public enum OrderType {
    MARKET,
    LIMIT,
    STOP_LOSS,         // STOPLOSS_LIMIT
    STOP_LOSS_MARKET
}
