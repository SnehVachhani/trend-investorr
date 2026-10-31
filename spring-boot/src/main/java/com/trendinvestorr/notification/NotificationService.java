package com.trendinvestorr.notification;

import org.springframework.stereotype.Service;

import java.util.concurrent.atomic.AtomicInteger;

/**
 * Domain-facing notification facade — port of Python {@code Notifier}.
 *
 * <p>Open–Closed: the facade depends on pluggable transports (Console + Telegram).
 * Adding a channel (e.g. e-mail) only requires injecting another transport bean —
 * callers keep depending on {@link NotificationPort}. The facade also tracks the
 * stats surfaced by Python {@code Notifier.stats}.
 */
@Service
public class NotificationService implements NotificationPort {

    private final ConsoleNotifier console;
    private final TelegramNotifier telegram;
    private final AtomicInteger sent = new AtomicInteger(0);

    public NotificationService(ConsoleNotifier console, TelegramNotifier telegram) {
        this.console = console;
        this.telegram = telegram;
    }

    @Override
    public void send(Notification notification) {
        // Always emit to console; best-effort to Telegram.
        console.send(notification);
        if (telegram.isConfigured()) {
            telegram.send(notification);
        }
        sent.incrementAndGet();
    }

    public java.util.Map<String, Object> stats() {
        return java.util.Map.of(
                "notifications_sent", sent.get(),
                "telegram_configured", telegram.isConfigured()
        );
    }
}
