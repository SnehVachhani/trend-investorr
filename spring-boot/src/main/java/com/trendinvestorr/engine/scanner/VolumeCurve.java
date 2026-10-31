package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.MarketConstants;

import java.time.LocalTime;

/**
 * Intraday cumulative volume-distribution curve — the expected fraction of
 * full-day volume that should have traded by a given IST clock time.
 *
 * <p>Pure, stateless, single-responsibility: given a {@link LocalTime}, return
 * the interpolated expected cumulative-volume fraction in {@code [0,1]}. Kept
 * separate from {@link VolumeScanner} so the math is testable in isolation
 * and the scanner stays focused on detection.
 *
 * <p>Port of the {@code _VOLUME_CURVE} dict + {@code _get_expected_volume_pct}
 * interpolation in {@code engine/volume_scanner.py}.
 */
public final class VolumeCurve {

    private VolumeCurve() {}

    /** hour (IST) → cumulative fraction of daily volume expected by that hour. */
    private static final int[] HOURS   = { 9, 10, 11, 12, 13, 14, 15, 16 };
    private static final double[] FRAC = {0.08, 0.20, 0.35, 0.48, 0.60, 0.75, 0.92, 1.00};

    /**
     * @return expected cumulative-volume fraction for {@code now} (IST).
     *         Outside market hours returns {@code 1.0} (treat as full day).
     */
    public static double expectedFraction(LocalTime now) {
        if (now == null) return 1.0;
        if (now.isBefore(MarketConstants.MARKET_OPEN) || now.isAfter(MarketConstants.MARKET_CLOSE)) {
            return 1.0;
        }
        int hour = now.getHour();
        int minute = now.getMinute();

        // Hour not in curve → after close.
        int idx = indexOf(hour);
        if (idx < 0) return 1.0;

        double basePct = idx == 0 ? 0.0 : FRAC[idx - 1];
        double nextPct = FRAC[idx];
        double minuteFraction = minute / 60.0;
        return basePct + (nextPct - basePct) * minuteFraction;
    }

    private static int indexOf(int hour) {
        for (int i = 0; i < HOURS.length; i++) if (HOURS[i] == hour) return i;
        return -1;
    }
}
