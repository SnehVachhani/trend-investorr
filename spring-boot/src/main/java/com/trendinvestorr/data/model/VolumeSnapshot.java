package com.trendinvestorr.data.model;

import java.time.Instant;

/**
 * Point-in-time volume reading for spike detection.
 * Port of {@code data.models.market_data.VolumeSnapshot}.
 */
public record VolumeSnapshot(
        String symbol,
        Instant timestamp,
        long cumulativeVolume,   // today's cumulative volume so far
        long avgVolume30d,
        long avgVolume90d
) {
    /** Current volume as a multiple of the 30-day average. */
    public double volumeRatio30d() {
        if (avgVolume30d == 0) return 0.0;
        return (double) cumulativeVolume / avgVolume30d;
    }

    /** Current volume as a multiple of the 90-day average. */
    public double volumeRatio90d() {
        if (avgVolume90d == 0) return 0.0;
        return (double) cumulativeVolume / avgVolume90d;
    }
}
