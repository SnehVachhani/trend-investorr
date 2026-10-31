package com.trendinvestorr.core.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Telegram bot credentials, bound from {@code app.telegram.*} in {@code application.yml}.
 * Mirrors the constructor args of Python {@code Notifier(__init__(telegram_token, telegram_chat_id))}.
 */
@ConfigurationProperties(prefix = "app.telegram")
public class TelegramProperties {

    private String botToken = "";
    private String chatId = "";

    public String getBotToken() { return botToken; }
    public void setBotToken(String botToken) { this.botToken = botToken; }

    public String getChatId() { return chatId; }
    public void setChatId(String chatId) { this.chatId = chatId; }

    public boolean isConfigured() {
        return botToken != null && !botToken.isBlank()
                && chatId != null && !chatId.isBlank();
    }
}
