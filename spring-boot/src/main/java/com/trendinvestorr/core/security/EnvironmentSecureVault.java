package com.trendinvestorr.core.security;

import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * {@link SecureVault} backed by the Spring {@link Environment} — secrets are
 * read from {@code application.yml} / external config / environment variables
 * on demand, with no plaintext caching. This is the production default wiring;
 * the encryption-backed file vault port can be plugged in later without
 * changing dependents (DIP).
 *
 * <p>Port of the read path of {@code core/security/vault.py} {@code SecureVault}.
 */
@Component
public class EnvironmentSecureVault implements SecureVault {

    private final Environment env;

    public EnvironmentSecureVault(Environment env) {
        this.env = env;
    }

    @Override
    public String getCredential(String key) {
        if (key == null || key.isBlank()) return null;
        // Try the literal key first, then common prefixed aliases.
        String v = env.getProperty(key);
        if (v != null && !v.isBlank()) return v;
        v = env.getProperty("app.secrets." + key);
        if (v != null && !v.isBlank()) return v;
        return env.getProperty(key.toLowerCase());
    }
}
