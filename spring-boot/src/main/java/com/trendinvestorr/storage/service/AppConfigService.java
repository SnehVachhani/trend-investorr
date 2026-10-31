package com.trendinvestorr.storage.service;

import com.trendinvestorr.storage.entity.AppConfigEntity;
import com.trendinvestorr.storage.repository.AppConfigRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Runtime-tunable parameter reader/writer. Replaces Python {@code AppConfigStore}.
 *
 * <p>Static secrets live in {@code application.yml} / {@link com.trendinvestorr.core.config.AppProperties}.
 * Runtime-tunable scanner/risk/algorithm params live in the {@code app_config} DB
 * table (seeded by the V1 Flyway migration) and are accessed here with an in-memory
 * cache that reloads on demand via {@link #reload()}.
 *
 * <p>Caller patterns mirror the Python source:
 * <pre>
 *   configStore.get("analyzer_type", "static");
 *   configStore.getFloat("spike_multiplier", 2.0);
 *   configStore.getInt("max_open_positions", 20);
 *   configStore.getBool("...", false);
 * </pre>
 */
@Service
public class AppConfigService {

    private static final Logger log = LoggerFactory.getLogger(AppConfigService.class);

    private final AppConfigRepository repository;
    private final Map<String, String> cache = new ConcurrentHashMap<>();
    private volatile boolean loaded = false;

    public AppConfigService(AppConfigRepository repository) {
        this.repository = repository;
    }

    // ── Reload / cache ──────────────────────────────────────────

    /** Loads (or reloads) all rows into the in-memory cache. */
    public synchronized void reload() {
        cache.clear();
        repository.findAllOrdered().forEach(row -> cache.put(row.getKey(), row.getValue()));
        loaded = true;
        log.debug("AppConfig reloaded: {} keys", cache.size());
    }

    private void ensureCache() {
        if (!loaded) {
            reload();
        }
    }

    /** Returns the raw string value for a key, or the default if not present. */
    public String get(String key, String defaultValue) {
        ensureCache();
        return cache.getOrDefault(key, defaultValue);
    }

    public double getFloat(String key, double defaultValue) {
        try {
            return Double.parseDouble(get(key, String.valueOf(defaultValue)));
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }

    public int getInt(String key, int defaultValue) {
        try {
            return (int) Double.parseDouble(get(key, String.valueOf(defaultValue)));
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }

    public boolean getBool(String key, boolean defaultValue) {
        String v = get(key, String.valueOf(defaultValue)).toLowerCase();
        return v.equals("true") || v.equals("1") || v.equals("yes") || v.equals("on");
    }

    /** Returns all currently-cached keys/values (read-only snapshot). */
    public Map<String, String> snapshot() {
        ensureCache();
        return Map.copyOf(cache);
    }

    // ── Writes ──────────────────────────────────────────────────

    /**
     * Upserts a runtime-config value. Bumps the in-memory cache too.
     */
    @org.springframework.transaction.annotation.Transactional
    public void set(String key, String value) {
        Optional<AppConfigEntity> existing = repository.findByKey(key);
        if (existing.isPresent()) {
            AppConfigEntity row = existing.get();
            row.setValue(value);
            repository.save(row);
        } else {
            AppConfigEntity row = new AppConfigEntity();
            row.setKey(key);
            row.setValue(value);
            row.setDataType(inferDataType(value));
            row.setCategory("general");
            repository.save(row);
        }
        cache.put(key, value);
    }

    /**
     * Convenience: typed update.
     */
    public void set(String key, double value) {
        set(key, String.valueOf(value));
    }

    public void set(String key, int value) {
        set(key, String.valueOf(value));
    }

    public void set(String key, boolean value) {
        set(key, value ? "true" : "false");
    }

    private static String inferDataType(String value) {
        if (value == null || value.isBlank()) return "string";
        if (value.equalsIgnoreCase("true") || value.equalsIgnoreCase("false")) return "bool";
        try { Double.parseDouble(value); return value.contains(".") ? "float" : "int"; }
        catch (NumberFormatException e) { return "string"; }
    }

    /** Returns the timestamp of the most recently updated row (or null). */
    public Optional<Instant> lastUpdatedAt() {
        return repository.findAllOrdered().stream()
                .map(AppConfigEntity::getUpdatedAt)
                .max(Instant::compareTo);
    }
}
