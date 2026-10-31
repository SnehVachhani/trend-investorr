package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.AnalysisAction;
import com.trendinvestorr.core.constants.MarketConstants;

import java.time.Instant;
import java.util.Map;

/**
 * Output from any SignalAnalyzer implementation (static / Gemini / composite).
 * Port of {@code data.models.market_data.AnalysisResult}.
 */
public record AnalysisResult(
        AnalysisAction action,           // BUY / SELL / SKIP / HOLD
        double confidence,                // 0.0 - 1.0
        String reasoning,                  // human-readable explanation
        String analyzerName,
        String signalSymbol,
        Map<String, Object> indicators,
        String rawResponse                // raw AI response (Gemini only)
) {
    public AnalysisResult {
        if (signalSymbol == null) signalSymbol = "";
        if (indicators == null) indicators = Map.of();
        if (rawResponse == null) rawResponse = "";
    }

    public boolean isActionable() {
        return action == AnalysisAction.BUY || action == AnalysisAction.SELL;
    }

    public String confidencePct() {
        return String.format("%.0f%%", confidence * 100);
    }

    /** Convenience factory for a SKIP from a named analyzer. */
    public static AnalysisResult skip(String analyzerName, String reason) {
        return new AnalysisResult(
                AnalysisAction.SKIP, 0.0, reason, analyzerName, "", Map.of(), "");
    }
}
