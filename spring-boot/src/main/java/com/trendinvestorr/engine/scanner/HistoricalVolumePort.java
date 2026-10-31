package com.trendinvestorr.engine.scanner;

/**
 * Read-only port for historical volume averages used by the VolumeScanner.
 *
 * <p>Defined as an interface (rather than a direct dependency on
 * {@code CandleRepository}) so the scanner depends on an abstraction, not a
 * concrete data source — adherence to the Dependency Inversion Principle. The
 * default implementation computes {@code (avg_30d, avg_90d)} from the daily
 * candle table; tests can swap in an in-memory stub.
 *
 * <p>Port of the {@code set_historical_volumes(...)} dict fed to the Python
 * {@code VolumeScanner}.
 */
public interface HistoricalVolumePort {

    /**
     * @return {@code (avg_30d, avg_90d)} volumes for the given token, or
     *         {@code null} when no history is available.
     */
    long[] averagesForToken(String token);
}
