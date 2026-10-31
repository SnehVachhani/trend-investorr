package com.trendinvestorr.strategy.algorithm;

/**
 * State of a trading algorithm. Port of {@code strategies.base.AlgorithmState}.
 */
public enum AlgorithmState {
    IDLE,             // No active setup, just listening
    WATCHING,         // Setup detected, waiting for entry conditions
    PENDING_ENTRY,   // Entry conditions met, waiting for next candle/tick to enter
    IN_TRADE,         // Position is open, managing the trade
    PENDING_EXIT      // Exit conditions met, waiting to close
}
