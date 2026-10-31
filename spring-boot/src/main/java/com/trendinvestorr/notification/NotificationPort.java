package com.trendinvestorr.notification;

import java.time.Instant;
import java.util.Map;

/**
 * Out-of-process notification contract — port of {@code notifications/notifier.py}.
 *
 * <p>Dependency Inversion: callers depend on this abstraction, not on any concrete
 * channel (Telegram, console, e-mail, …). New channels plug in as additional
 * {@code @Component} implementations of this interface without modifying callers
 * (Open–Closed). Each {@link #send(Notification)} hands one logical event to the
 * transport layer; failures are swallowed and logged by each impl so notification
 * outages never break the trading path.
 */
public interface NotificationPort {

    /** Push a fully-formed notification through all configured transports. */
    void send(Notification notification);

    record Notification(
            NotificationType type,
            NotificationPriority priority,
            String title,
            String message,
            Map<String, Object> data,
            Instant timestamp
    ) {}

    /** Convenience builders mirroring the Python {@code Notifier} helper methods. */
    default void signalAlert(String symbol, String signalType, double score,
                             double volumeRatio, double price, String strength) {
        send(new Notification(
                NotificationType.SIGNAL,
                NotificationPriority.MEDIUM,
                "Signal: " + signalType + " " + symbol,
                "score=" + score + " volRatio=" + volumeRatio + " price=" + price + " strength=" + strength,
                Map.of("symbol", symbol, "signal_type", signalType, "score", score),
                Instant.now()));
    }

    default void tradeAlert(String symbol, String side, int quantity,
                            double fillPrice, boolean isPaper, double pnl) {
        var mode = isPaper ? "PAPER " : "LIVE ";
        var priority = isPaper ? NotificationPriority.MEDIUM : NotificationPriority.HIGH;
        send(new Notification(
                NotificationType.TRADE,
                priority,
                mode + side + " " + symbol,
                "qty=" + quantity + " price=" + fillPrice + " pnl=" + pnl,
                Map.of("symbol", symbol, "side", side, "qty", quantity, "pnl", pnl, "paper", isPaper),
                Instant.now()));
    }

    default void riskAlert(String title, String details) {
        send(new Notification(
                NotificationType.RISK,
                NotificationPriority.CRITICAL,
                "RISK: " + title,
                details,
                Map.of(),
                Instant.now()));
    }

    default void systemAlert(String title, String message, NotificationPriority priority) {
        send(new Notification(
                NotificationType.SYSTEM,
                priority,
                title,
                message,
                Map.of(),
                Instant.now()));
    }

    default void dailySummary(String dateStr, double realizedPnl, double unrealizedPnl,
                              int totalTrades, int winning, int losing,
                              double portfolioValue, int signalsGenerated) {
        double totalPnl = realizedPnl + unrealizedPnl;
        double winRate = totalTrades == 0 ? 0.0 : (winning * 100.0 / totalTrades);
        send(new Notification(
                NotificationType.DAILY,
                NotificationPriority.MEDIUM,
                "Daily Summary — " + dateStr,
                "pnl=" + totalPnl + " trades=" + totalTrades + " winRate=" + winRate
                        + "% portfolio=" + portfolioValue + " signals=" + signalsGenerated,
                Map.of(
                        "realized_pnl", realizedPnl,
                        "unrealized_pnl", unrealizedPnl,
                        "total_trades", totalTrades,
                        "winning", winning,
                        "losing", losing,
                        "portfolio_value", portfolioValue,
                        "signals_generated", signalsGenerated
                ),
                Instant.now()));
    }
}
