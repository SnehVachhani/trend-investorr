package com.trendinvestorr.notification;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.trendinvestorr.core.config.TelegramProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;

/**
 * Telegram Bot transport for {@link NotificationPort}.
 *
 * <p>Single Responsibility: only knows how to render a {@link Notification} as Markdown
 * and POST it to the Telegram {@code sendMessage} API. It depends on
 * {@link TelegramProperties} (configuration) and nothing else from the domain, so it
 * can be swapped or mocked without touching callers.
 *
 * <p>Failures are caught and logged at debug level — matching the Python behavior of
 * "never raise from a notification path". Configured-ness is decided by
 * {@link TelegramProperties#isConfigured()}.
 */
@Component
public class TelegramNotifier {

    private static final Logger log = LoggerFactory.getLogger(TelegramNotifier.class);
    private static final String API_BASE = "https://api.telegram.org/bot";
    private static final String SEND_MESSAGE = "/sendMessage";

    private final TelegramProperties properties;
    private final ObjectMapper objectMapper;
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    public TelegramNotifier(TelegramProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;
    }

    public boolean isConfigured() {
        return properties.isConfigured();
    }

    /** Send a notification; returns {@code true} only on a 2xx response. */
    public boolean send(NotificationPort.Notification n) {
        if (!isConfigured()) {
            return false;
        }
        String text = "*" + n.title() + "*\n" + n.message();
        Map<String, Object> payload = Map.of(
                "chat_id", properties.getChatId(),
                "text", text,
                "parse_mode", "Markdown"
        );
        try {
            String body = objectMapper.writeValueAsString(payload);
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(API_BASE + properties.getBotToken() + SEND_MESSAGE))
                    .header("Content-Type", "application/json")
                    .timeout(Duration.ofSeconds(5))
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .build();
            HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() >= 200 && resp.statusCode() < 300) {
                return true;
            }
            log.debug("Telegram sendMessage failed status={} body={}", resp.statusCode(), resp.body());
            return false;
        } catch (Exception e) {
            log.debug("Telegram notify error", e);
            return false;
        }
    }
}
