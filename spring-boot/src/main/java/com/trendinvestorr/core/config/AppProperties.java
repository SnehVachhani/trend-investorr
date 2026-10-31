package com.trendinvestorr.core.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * Spring Boot configuration root. Port of {@code config.settings.Settings}.
 *
 * <p>Static settings (secrets, connection strings) bind here from {@code application.yml} /
 * environment variables. Runtime-tunable values (scanner thresholds, algo params) remain
 * in the {@code app_config} DB table and are accessed via {@link AppConfigService}.
 *
 * <p>This replaces the singleton {@code Settings()} class — Spring manages a single instance
 * we can inject anywhere.
 *
 * @param env       {@code APP_ENV} (development / staging / production)
 * @param logLevel  loguru-style log level
 * @param tradingMode  PAPER (default) or LIVE
 * @param totalCapital starting capital (₹) — defaults to 1 crore
 */
@Component
@ConfigurationProperties(prefix = "app")
public record AppProperties(
        String env,
        String logLevel,
        String tradingMode,
        double totalCapital,
        AngelProperties angel,
        GeminiProperties gemini,
        VaultProperties vault,
        TelegramProperties telegram
) {
    public AppProperties {
        if (env == null) env = "development";
        if (logLevel == null) logLevel = "INFO";
        if (tradingMode == null) tradingMode = "PAPER";
        if (angel == null) angel = new AngelProperties(null, null, null, null);
        if (gemini == null) gemini = new GeminiProperties(null);
        if (vault == null) vault = new VaultProperties(null, null);
        if (telegram == null) telegram = new TelegramProperties(null, null);
    }

    public boolean isProduction() {
        return "production".equalsIgnoreCase(env);
    }

    public boolean hasAngelCredentials() {
        return angel != null
                && angel.apiKey() != null && !angel.apiKey().isBlank()
                && angel.clientId() != null && !angel.clientId().isBlank()
                && angel.password() != null && !angel.password().isBlank()
                && angel.totpSecret() != null && !angel.totpSecret().isBlank();
    }

    public boolean hasGeminiKey() {
        return gemini != null && gemini.apiKey() != null && !gemini.apiKey().isBlank();
    }

    /**
     * Static credentials for Angel One SmartAPI. Replaces the {@code ANGEL_*} env vars.
     */
    public record AngelProperties(
            String apiKey,
            String clientId,
            String password,
            String totpSecret
    ) {}

    /**
     * Optional Google Gemini API key for the GeminiAnalyzer.
     */
    public record GeminiProperties(String apiKey) {}

    /**
     * Master key + path for the encrypted credential vault.
     */
    public record VaultProperties(String masterKey, String path) {}

    /**
     * Telegram bot credentials used by {@code notification.Notifier}.
     */
    public record TelegramProperties(String botToken, String chatId) {}
}
