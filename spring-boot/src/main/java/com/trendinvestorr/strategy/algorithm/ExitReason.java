package com.trendinvestorr.strategy.algorithm;

/**
 * Why a trade was exited. Port of {@code strategies.base.ExitReason}.
 */
public enum ExitReason {
    PROFIT_TARGET,
    STOP_LOSS,
    TRAILING_STOP,
    TIME_STOP,
    INDICATOR_EXIT,
    MANUAL,
    SQUARE_OFF          // End-of-day auto square-off
}
