package com.trendinvestorr.analyzer;

import com.trendinvestorr.data.model.AnalysisResult;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.data.model.Signal;

import java.util.List;

/**
 * Strategy contract for every signal analyzer (static rules, Gemini AI,
 * composite).
 *
 * <p>This is the inversion point (DIP): the {@code StrategyEngine} and
 * {@code AnalyzerRegistry} depend only on this abstraction, never on a
 * concrete analyzer — adding a new analyzer is a new implementation plugged
 * in at wiring time (OCP).
 *
 * <p>Contract (port of {@code analyzers/base.py} {@code SignalAnalyzer}):
 * <ul>
 *   <li>{@link #analyze} MUST NOT throw — return a SKIP result on failure.</li>
 *   <li>{@link #healthCheck} returns {@code false} when the analyzer is
 *       degraded (e.g. API key missing, service down).</li>
 *   <li>{@link #name} is a stable identifier used for routing & attribution.</li>
 * </ul>
 */
public interface SignalAnalyzer {

    /**
     * Analyze a signal against recent historical candles.
     *
     * @param signal  the signal emitted by the signal generator
     * @param candles recent daily candles (typically 30–90 days)
     * @return an {@link AnalysisResult} (never {@code null}); SKIP on error
     */
    AnalysisResult analyze(Signal signal, List<Candle> candles);

    /** True when this analyzer can process signals right now. */
    boolean healthCheck();

    /** Stable identifier, e.g. {@code "static"} / {@code "gemini"}. */
    String name();
}
