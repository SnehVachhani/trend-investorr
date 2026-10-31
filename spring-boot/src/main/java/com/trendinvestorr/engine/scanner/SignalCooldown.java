package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.MarketConstants;

import java.time.Duration;
import java.time.Instant;
import java.time.ZonedDateTime;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Small, single-responsibility helper that tracks per-token signal cooldowns.
 *
 * <p>Extracted into its own class so that every scanner (volume, momentum, …)
 * shares one identical cooldown policy instead of re-implementing the bookkeeping
 * inline — keeps each scanner focused on detection logic only (SRP).
 *
 * <p>Port of the {@code _signal_cooldowns} / {@code _cooldowns} dicts shared by
 * the Python {@code VolumeScanner} and {@code MomentumScanner}.
 */
public class SignalCooldown {

    private final Map<String, Instant> lastSignalAt = new ConcurrentHashMap<>();
    private final Duration cooldown;

    public SignalCooldown(Duration cooldown) {
        this.cooldown = cooldown == null ? Duration.ofMinutes(30) : cooldown;
    }

    /** Record that a signal was just emitted for {@code token}. */
    public void mark(String token) {
        lastSignalAt.put(token, now());
    }

    /** True when {@code token} is still within its cooldown window. */
    public boolean isOnCooldown(String token) {
        Instant last = lastSignalAt.get(token);
        if (last == null) return false;
        return Duration.between(last, now()).compareTo(cooldown) < 0;
    }

    /** Drop all cooldown records (used at the start of a new trading day). */
    public void reset() {
        lastSignalAt.clear();
    }

    private static Instant now() {
        return ZonedDateTime.now(MarketConstants.IST).toInstant();
    }
}
