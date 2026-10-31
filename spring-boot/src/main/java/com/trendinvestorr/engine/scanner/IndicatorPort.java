package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.data.model.TechnicalIndicators;
import com.trendinvestorr.storage.entity.AbstractCandleEntity;

import java.util.List;

/**
 * Read-only port for retrieving the latest pre-computed technical indicators
 * for a token — the abstraction the {@code MomentumScanner} (and other
 * indicator-driven scanners) depends on rather than importing the indicator
 * computation directly.
 *
 * <p>Indicators are computed during the pre-market warm-up batch and cached
 * in-memory (port of the {@code _indicators} dict in the Python
 * {@code MomentumScanner}); scanners consume them on the hot path with zero
 * DB latency.
 *
 * <p>DIP: scanners depend on this interface, the concrete
 * {@code IndicatorService} / cache implementation plugs in at wiring time.
 */
public interface IndicatorPort {

    /**
     * @return the latest {@link TechnicalIndicators} for {@code token}, or
     *         {@code null} when none are available.
     */
    TechnicalIndicators indicatorsForToken(String token);

    /**
     * Compute and persist indicators for a single stock from a daily candle slice.
     * The impl owns the formula math + storage write; the data-ingestion layer
     * only supplies {@code stockId} and recent candles.
     *
     * @param stockId target stock id
     * @param daily   recent daily candle rows (oldest-first)
     */
    void computeAndSave(long stockId, List<? extends AbstractCandleEntity> daily);
}

