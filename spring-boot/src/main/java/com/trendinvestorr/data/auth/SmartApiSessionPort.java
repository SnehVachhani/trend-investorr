package com.trendinvestorr.data.auth;

/**
 * SmartAPI session lifecycle port — abstracts Angel One 2FA login / re-auth.
 *
 * <p>Consumers (HistoricalFetcher, WebSocketManager, OrderManager) depend on this port,
 * not on the SmartConnect SDK, so the auth strategy can be swapped (e.g. test stub,
 * token cache) without touching them.
 *
 * <p>Port of {@code data.auth.smartapi_auth.SmartAPISession}.
 */
public interface SmartApiSessionPort {

    /** Perform TOTP + password login; returns {@code true} on success. */
    boolean login() throws com.trendinvestorr.core.exceptions.AuthenticationError;

    /** Re-login if the session is stale (SmartAPI midnight IST logout). */
    void ensureAuthenticated() throws com.trendinvestorr.core.exceptions.SessionExpiredError;

    /** Underlying broker SDK handle; never {@code null} once authenticated. */
    Object api();

    void logout();

    boolean isAuthenticated();

    String clientId();

    String feedToken();

    java.time.Instant lastLoginAt();

    int loginCount();
}
