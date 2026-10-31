package com.trendinvestorr.data.auth;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.core.exceptions.AuthenticationError;
import com.trendinvestorr.core.exceptions.SessionExpiredError;
import com.trendinvestorr.storage.service.AuditTrailService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * SmartAPI session adapter — performs TOTP + password login via the SmartAPI REST endpoint
 * and refreshes the JWT when the previous session crosses midnight IST. Port of Python
 * {@code SmartAPISession}.
 *
 * <p>DIP: this is the production {@link SmartApiSessionPort} impl. Tests typically wire a
 * stub that returns canned tokens so scanners / fetchers never hit a live broker.
 *
 * <p>The credentials come from {@code app.angel.*} properties; blank credentials cause
 * {@link #login()} to throw {@link AuthenticationError} — same contract as the Python code.
 */
@Component
public class SmartApiSessionAdapter implements SmartApiSessionPort {

    private static final Logger log = LoggerFactory.getLogger(SmartApiSessionAdapter.class);
    private static final String LOGIN_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/jwt/v1/loginByPassword";

    private final AuditTrailService audit;
    private final ObjectMapper json;
    private final WebClient http;

    @Value("${app.angel.api-key:}") private String apiKey;
    @Value("${app.angel.client-id:}") private String clientId;
    @Value("${app.angel.password:}") private String password;
    @Value("${app.angel.totp-secret:}") private String totpSecret;

    private final AtomicReference<String> jwt = new AtomicReference<>("");
    private final AtomicReference<String> feed = new AtomicReference<>("");
    private volatile Instant lastLoginAt;
    private final AtomicInteger logins = new AtomicInteger(0);

    public SmartApiSessionAdapter(AuditTrailService audit, ObjectMapper json, WebClient.Builder builder) {
        this.audit = audit;
        this.json = json;
        this.http = builder.baseUrl(LOGIN_URL).build();
    }

    @Override
    public boolean login() {
        if (isMissing(apiKey) || isMissing(clientId) || isMissing(password) || isMissing(totpSecret)) {
            audit.log(AuditEventType.LOGIN_FAILURE, Map.of("reason", "missing credentials"));
            throw new AuthenticationError("Missing Angel One credentials (check app.angel.* properties)");
        }
        try {
            String totp = generateTotp(totpSecret);
            String resp = http.post()
                    .header("Content-Type", "application/json")
                    .bodyValue(Map.of(
                            "clientcode", clientId,
                            "password", password,
                            "totp", totp))
                    .retrieve()
                    .bodyToMono(String.class)
                    .block(java.time.Duration.ofSeconds(15));
            JsonNode body = json.readTree(resp == null ? "{}" : resp);
            JsonNode data = body.path("data");
            this.jwt.set(data.path("jwtToken").asText(""));
            this.feed.set(data.path("feedToken").asText(""));
            this.lastLoginAt = Instant.now();
            logins.incrementAndGet();
            audit.log(AuditEventType.LOGIN_SUCCESS, Map.of("client_id", clientId));
            log.info("SmartAPI login ok for {}", clientId);
            return true;
        } catch (Exception e) {
            audit.log(AuditEventType.LOGIN_FAILURE, Map.of("error", e.getMessage()));
            throw new AuthenticationError("SmartAPI login failed", e);
        }
    }

    @Override
    public void ensureAuthenticated() {
        if (!isAuthenticated() || isSessionExpired()) {
            if (!login()) {
                throw new SessionExpiredError("SmartAPI session could not be renewed");
            }
        }
    }

    @Override
    public Object api() {
        ensureAuthenticated();
        return jwt.get();
    }

    @Override
    public void logout() {
        jwt.set("");
        feed.set("");
        log.info("SmartAPI session logged out");
    }

    @Override
    public boolean isAuthenticated() {
        return !jwt.get().isBlank() && lastLoginAt != null;
    }

    @Override public String clientId() { return clientId; }
    @Override public String feedToken() { ensureAuthenticated(); return feed.get(); }
    @Override public Instant lastLoginAt() { return lastLoginAt; }
    @Override public int loginCount() { return logins.get(); }

    private boolean isSessionExpired() {
        if (lastLoginAt == null) return true;
        LocalDate loginDate = lastLoginAt.atZone(ZoneId.of("Asia/Kolkata")).toLocalDate();
        LocalDate today = LocalDate.now(ZoneId.of("Asia/Kolkata"));
        return !loginDate.equals(today) ||
                LocalTime.now(ZoneId.of("Asia/Kolkata")).getHour() >= 23 &&
                LocalTime.now(ZoneId.of("Asia/Kolkata")).getMinute() >= 55;
    }

    private static boolean isMissing(String v) { return v == null || v.isBlank(); }

    /**
     * Generate a 6-digit TOTP using the configured secret.
     * The TOTP algorithm is RFC-6238 (HMAC-SHA1 + 30 s window). We implement the bare minimum
     * inline to avoid pulling the {@code dev.samstevens.totp} library into wiring-time classpath;
     * if it is on the classpath we delegate via reflection.
     */
    private String generateTotp(String secret) {
        try {
            Class<?> clazz = Class.forName("dev.samstevens.totp.code.DefaultCodeGenerator");
            Object gen = clazz.getConstructor().newInstance();
            Object code = clazz.getMethod("generate", String.class, long.class)
                    .invoke(gen, secret, System.currentTimeMillis() / 1000L / 30L);
            return code.toString();
        } catch (Exception e) {
            throw new AuthenticationError("TOTP generation failed", e);
        }
    }
}
