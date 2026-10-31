package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.SignalSource;
import com.trendinvestorr.data.model.Observation;
import com.trendinvestorr.data.model.Tick;

import java.util.List;

/**
 * Single-responsibility contract for every market scanner in the engine.
 *
 * <p>A scanner consumes real-time {@link Tick} updates (and, where needed,
 * pre-computed indicators / historical volume) and emits zero or more
 * {@link Observation}s. It never places orders, persists state, or talks to
 * brokers — it only translates raw price/volume data into structured facts.
 *
 * <p>This interface is the inversion point (DIP) used by the
 * {@code AlgorithmManager} / {@code StrategyEngine} layer, which depends only
 * on the abstraction, never on a concrete scanner implementation.
 *
 * <p>Port of the implicit {@code Scanner} duck type used by the Python
 * {@code engine/*_scanner.py} modules (each had {@code on_tick} +
 * {@code get_signals} + {@code reset_daily}).
 */
public interface Scanner {

    /**
     * Process a real-time tick and return any observations produced.
     *
     * @param tick the latest price/volume update from the WebSocket feed
     * @return an unmodifiable list of observations (never {@code null}; empty
     *         when nothing was detected this tick)
     */
    List<Observation> onTick(Tick tick);

    /** Which scanner produced these observations — used for attribution & confluence. */
    SignalSource source();

    /** Human-readable name, e.g. {@code "VolumeScanner"}. */
    default String name() {
        return getClass().getSimpleName();
    }

    /** Reset all per-stock state for a new trading day. */
    void resetDaily();
}
