package com.trendinvestorr.analyzer;

import com.trendinvestorr.core.constants.AnalysisAction;
import com.trendinvestorr.core.constants.CompositeStrategy;
import com.trendinvestorr.data.model.AnalysisResult;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.data.model.Signal;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Combines multiple {@link SignalAnalyzer} results via a configurable
 * voting / merging {@link CompositeStrategy}.
 *
 * <p>Each strategy is its own private combinator method (SRP/OCP): adding a
 * new strategy means adding a branch, not rewriting the orchestration.
 *
 * <p>Port of {@code analyzers/composite_analyzer.py}.
 */
public class CompositeAnalyzer implements SignalAnalyzer {

    private static final Logger log = LoggerFactory.getLogger(CompositeAnalyzer.class);

    private final List<SignalAnalyzer> analyzers;
    private final double[] weights;
    private final CompositeStrategy strategy;

    public CompositeAnalyzer(List<SignalAnalyzer> analyzers, List<Double> weights, CompositeStrategy strategy) {
        if (analyzers == null || analyzers.isEmpty()) {
            throw new IllegalArgumentException("CompositeAnalyzer requires at least one analyzer");
        }
        this.analyzers = List.copyOf(analyzers);
        this.strategy = strategy == null ? CompositeStrategy.WEIGHTED : strategy;
        if (weights == null) {
            this.weights = new double[analyzers.size()];
            for (int i = 0; i < this.weights.length; i++) this.weights[i] = 1.0 / analyzers.size();
        } else {
            if (weights.size() != analyzers.size()) {
                throw new IllegalArgumentException("weights length must match analyzers length");
            }
            this.weights = new double[weights.size()];
            for (int i = 0; i < weights.size(); i++) this.weights[i] = weights.get(i);
        }
        log.info("CompositeAnalyzer: strategy={}, analyzers={}, weights={}",
                this.strategy, analyzers.stream().map(SignalAnalyzer::name).toList(),
                java.util.Arrays.stream(this.weights).boxed()
                        .map(w -> String.format("%.0f%%", w * 100)).toList());
    }

    @Override public String name() { return "composite"; }

    @Override
    public boolean healthCheck() {
        for (SignalAnalyzer a : analyzers) if (a.healthCheck()) return true;
        return false;
    }

    @Override
    public AnalysisResult analyze(Signal signal, List<Candle> candles) {
        try {
            List<Weighted> results = new ArrayList<>();
            for (int i = 0; i < analyzers.size(); i++) {
                SignalAnalyzer a = analyzers.get(i);
                try {
                    if (!a.healthCheck()) { log.debug("Analyzer '{}' unhealthy — skipping", a.name()); continue; }
                    AnalysisResult r = a.analyze(signal, candles);
                    results.add(new Weighted(r, weights[i]));
                } catch (Exception e) {
                    log.error("Analyzer '{}' crashed: {}", a.name(), e.getMessage());
                }
            }
            if (results.isEmpty()) {
                return AnalysisResult.skip(name(), "All analyzers failed or unhealthy");
            }
            List<Weighted> actionable = new ArrayList<>();
            for (Weighted w : results) if (w.r.action() != AnalysisAction.SKIP) actionable.add(w);
            if (actionable.isEmpty()) {
                String joined = results.stream().map(w -> "[" + w.r.analyzerName() + "] " + w.r.reasoning())
                        .reduce((x, y) -> x + " | " + y).orElse("");
                return new AnalysisResult(AnalysisAction.SKIP, 0.0, joined, name(), signal.symbol(), Map.of(), "");
            }
            return switch (strategy) {
                case UNANIMOUS -> unanimous(actionable, signal.symbol());
                case MAJORITY -> majority(actionable, signal.symbol());
                case PRIMARY -> primary(results, signal.symbol());
                case WEIGHTED -> weighted(actionable, signal.symbol());
            };
        } catch (Exception e) {
            log.error("Composite analyzer error: {}", e.getMessage());
            return AnalysisResult.skip(name(), "Composite analysis error: " + e.getMessage());
        }
    }

    private AnalysisResult unanimous(List<Weighted> rs, String symbol) {
        long distinct = rs.stream().map(w -> w.r.action()).distinct().count();
        if (distinct == 1) {
            AnalysisAction action = rs.get(0).r.action();
            double sumW = rs.stream().mapToDouble(w -> w.weight).sum();
            double avg = rs.stream().mapToDouble(w -> w.r.confidence() * w.weight).sum() / sumW;
            String reasoning = rs.stream().map(w -> "[" + w.r.analyzerName() + "] " + w.r.reasoning())
                    .reduce((x, y) -> x + " | " + y).orElse("");
            return new AnalysisResult(action, avg, "UNANIMOUS: " + reasoning, name(), symbol, mergeIndicators(rs), "");
        }
        String reasoning = rs.stream().map(w -> "[" + w.r.analyzerName() + "] " + w.r.action() + " ("
                + String.format("%.0f%%", w.r.confidence() * 100) + ")").reduce((x, y) -> x + " | " + y).orElse("");
        return new AnalysisResult(AnalysisAction.SKIP, 0.0, "No consensus — " + reasoning, name(), symbol, Map.of(), "");
    }

    private AnalysisResult majority(List<Weighted> rs, String symbol) {
        Map<AnalysisAction, Integer> votes = new HashMap<>();
        for (Weighted w : rs) votes.merge(w.r.action(), 1, Integer::sum);
        AnalysisAction winning = votes.entrySet().stream()
                .max(Map.Entry.comparingByValue()).map(Map.Entry::getKey).orElse(AnalysisAction.SKIP);
        List<Weighted> winners = rs.stream().filter(w -> w.r.action() == winning).toList();
        double sumW = winners.stream().mapToDouble(w -> w.weight).sum();
        double avg = winners.stream().mapToDouble(w -> w.r.confidence() * w.weight).sum() / sumW;
        String reasoning = rs.stream().map(w -> "[" + w.r.analyzerName() + "] " + w.r.reasoning())
                .reduce((x, y) -> x + " | " + y).orElse("");
        return new AnalysisResult(winning, avg,
                "MAJORITY (" + votes.get(winning) + "/" + rs.size() + "): " + reasoning,
                name(), symbol, mergeIndicators(rs), "");
    }

    private AnalysisResult weighted(List<Weighted> rs, String symbol) {
        double rawTotal = rs.stream().mapToDouble(w -> w.weight).sum();
        final double totalW = rawTotal == 0 ? 1.0 : rawTotal;
        Map<AnalysisAction, Double> scores = new HashMap<>();
        for (Weighted w : rs) scores.merge(w.r.action(), w.r.confidence() * (w.weight / totalW), Double::sum);
        AnalysisAction winning = scores.entrySet().stream()
                .max(Map.Entry.comparingByValue()).map(Map.Entry::getKey).orElse(AnalysisAction.SKIP);
        double conf = 0.0;
        for (Weighted w : rs) if (w.r.action() == winning) conf += w.r.confidence() * (w.weight / totalW);
        String reasoning = rs.stream().map(w -> "[" + w.r.analyzerName() + "] " + w.r.action() + " ("
                + String.format("%.0f%%", w.r.confidence() * 100) + ", w=" + String.format("%.0f%%", w.weight / totalW * 100) + ")")
                .reduce((x, y) -> x + " | " + y).orElse("");
        return new AnalysisResult(winning, Math.min(conf, 1.0), "WEIGHTED: " + reasoning,
                name(), symbol, mergeIndicators(rs), "");
    }

    private AnalysisResult primary(List<Weighted> rs, String symbol) {
        AnalysisResult primary = rs.get(0).r;
        Map<String, Object> merged = new LinkedHashMap<>(primary.indicators());
        List<String> notes = new ArrayList<>();
        for (int i = 1; i < rs.size(); i++) {
            AnalysisResult r = rs.get(i).r;
            notes.add("[" + r.analyzerName() + "] " + r.action() + " ("
                    + String.format("%.0f%%", r.confidence() * 100) + ")");
            for (Map.Entry<String, Object> e : r.indicators().entrySet()) {
                merged.putIfAbsent(e.getKey(), e.getValue());
            }
        }
        String reasoning = primary.reasoning() + (notes.isEmpty() ? "" : " | Secondary: " + String.join(", ", notes));
        return new AnalysisResult(primary.action(), primary.confidence(), reasoning,
                name(), symbol, merged, "");
    }

    private static Map<String, Object> mergeIndicators(List<Weighted> rs) {
        Map<String, Object> merged = new LinkedHashMap<>();
        Map<String, Integer> seen = new HashMap<>();
        for (Weighted w : rs) {
            for (Map.Entry<String, Object> e : w.r.indicators().entrySet()) {
                String key = seen.containsKey(e.getKey())
                        ? w.r.analyzerName() + "_" + e.getKey() : e.getKey();
                merged.put(key, e.getValue());
                seen.merge(e.getKey(), 1, Integer::sum);
            }
        }
        return merged;
    }

    private record Weighted(AnalysisResult r, double weight) {}
}
