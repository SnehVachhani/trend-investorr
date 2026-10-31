package com.trendinvestorr.data.model;

import java.time.LocalDate;

/**
 * Daily profit and loss summary. Used for the 17:00 daily-summary Telegram notification.
 * Port of {@code data.models.market_data.DailyPnL}.
 */
public record DailyPnL(
        LocalDate date,
        double realizedPnl,
        double unrealizedPnl,
        int totalTrades,
        int winningTrades,
        int losingTrades,
        double maxDrawdown,
        boolean isPaper
) {
    public double totalPnl() {
        return realizedPnl + unrealizedPnl;
    }

    public double winRate() {
        if (totalTrades == 0) return 0.0;
        return ((double) winningTrades / totalTrades) * 100.0;
    }
}
