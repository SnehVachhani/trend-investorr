package com.trendinvestorr.core.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * Runtime-tunable scanner thresholds. Port of the values the original Python project
 * read via {@code config_store.get_*(...)} lookups in {@code main.py} Step 6.
 *
 * <p>These are bound from {@code scanner.*} in {@code application.yml}; they can also be
 * overridden at runtime by values stored in the {@code app_config} DB table (see
 * {@link AppConfigService}). Defaults match the Python defaults exactly.
 */
@Component
@ConfigurationProperties(prefix = "scanner")
public record ScannerProperties(
        VolumeProperties volume,
        double minPrice,
        double maxPrice,
        int minAvgVolume,
        double minConfluence,
        int minScanners,
        int maxEntriesPerCycle,
        int orbMinutes,
        double connorsRsiEntry,
        double connorsRsiExit,
        int connorsMaxHold
) {
    public ScannerProperties {
        if (volume == null) volume = new VolumeProperties(2.0);
        if (minConfluence == 0.0) minConfluence = 0.35;
        if (minScanners == 0) minScanners = 2;
        if (maxEntriesPerCycle == 0) maxEntriesPerCycle = 5;
        if (orbMinutes == 0) orbMinutes = 15;
        if (connorsRsiEntry == 0.0) connorsRsiEntry = 25.0;
        if (connorsRsiExit == 0.0) connorsRsiExit = 40.0;
        if (connorsMaxHold == 0) connorsMaxHold = 10;
    }

    public record VolumeProperties(double spikeMultiplier) {}
}
