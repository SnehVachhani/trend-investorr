package com.trendinvestorr.core.security;

/**
 * Read-only port for retrieving secrets (API keys, tokens) from the secure
 * vault. Defined as an abstraction so analyzers / fetchers depend on the
 * interface, not the concrete encryption-backed vault implementation (DIP).
 *
 * <p>Port of {@code core/security/vault.py} {@code SecureVault.get_credential}.
 */
public interface SecureVault {
    /** @return the credential for {@code key}, or {@code null} when absent. */
    String getCredential(String key);

    /** @return true when a usable credential exists for {@code key}. */
    default boolean hasCredential(String key) {
        String v = getCredential(key);
        return v != null && !v.isBlank();
    }
}
