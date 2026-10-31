package com.trendinvestorr.notification;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Resilient console transport — always logs the notification.
 *
 * <p>Maps Python's {@code _console_notify} ({@code loguru.warning}/info}.
 * It is independent of any external service, so it doubles as a no-op-safe fallback
 * when the Telegram channel is unconfigured or unavailable.
 */
@Component
public class ConsoleNotifier {

    private static final Logger log = LoggerFactory.getLogger(ConsoleNotifier.class);

    public void send(NotificationPort.Notification n) {
        var level = n.priority() == NotificationPriority.CRITICAL
                ? org.slf4j.event.Level.WARN
                : org.slf4j.event.Level.INFO;
        String line = "[" + n.priority() + "][" + n.type() + "] " + n.title() + " — " + n.message();
        if (level == org.slf4j.event.Level.WARN) {
            log.warn(line);
        } else {
            log.info(line);
        }
    }
}
