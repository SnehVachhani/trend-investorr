package com.trendinvestorr.analyzer;

import com.trendinvestorr.core.constants.CompositeStrategy;
import com.trendinvestorr.core.exceptions.ConfigurationError;
import com.trendinvestorr.core.security.SecureVault;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Factory + cache for {@link SignalAnalyzer} instances keyed by configuration
 * name. Reads the Gemini API key from the {@link SecureVault}; degrades
 * gracefully to static-only when the key is absent (so composite configs never
 * fail in secret-free environments / tests).
 *
 * <p>Known configs (port of {@code analyzers/registry.py}):
 * {@code static}, {@code gemini}, {@code composite},
 * {@code composite_conservative}, {@code composite_ai_primary}.
 *
 * <p>OCP: a new config is a new branch in {@link #build}; the wiring above
 * (which already injects every concrete {@code SignalAnalyzer} bean) is left
 * untouched.
 */
@Component
public class AnalyzerRegistry {

    private static final Logger log = LoggerFactory.getLogger(AnalyzerRegistry.class);

    public static final Set<String> KNOWN_CONFIGS = Set.of(
            "static", "gemini", "composite", "composite_conservative", "composite_ai_primary");

    private final SecureVault vault;
    private final StaticRuleAnalyzer staticAnalyzer;
    private final Map<String, SignalAnalyzer> cache = new ConcurrentHashMap<>();

    public AnalyzerRegistry(SecureVault vault, StaticRuleAnalyzer staticAnalyzer) {
        this.vault = vault;
        this.staticAnalyzer = staticAnalyzer;
        // Static is always available — pre-cache it.
        cache.put("static", staticAnalyzer);
    }

    /** Return a cached analyzer for the given config name, building it lazily. */
    public SignalAnalyzer create(String configName) {
        String name = configName == null || configName.isBlank() ? "static" : configName;
        return cache.computeIfAbsent(name, this::build);
    }

    private SignalAnalyzer build(String configName) {
        return switch (configName) {
            case "static" -> staticAnalyzer;
            case "gemini" -> buildGemini();
            case "composite" -> buildComposite(List.of(0.7, 0.3), CompositeStrategy.WEIGHTED);
            case "composite_conservative" -> buildComposite(List.of(0.5, 0.5), CompositeStrategy.UNANIMOUS);
            case "composite_ai_primary" -> buildComposite(List.of(0.3, 0.7), CompositeStrategy.WEIGHTED);
            default -> throw new ConfigurationError(
                    "Unknown analyzer config: '" + configName + "'. Known: " + KNOWN_CONFIGS);
        };
    }

    private SignalAnalyzer buildGemini() {
        // Gemini analyzer isn't ported yet (no AI client wired); fall back to static
        // so the registry stays usable. Logged once at creation.
        log.warn("GeminiAnalyzer not available — falling back to StaticRuleAnalyzer");
        return staticAnalyzer;
    }

    private SignalAnalyzer buildComposite(List<Double> weights, CompositeStrategy strategy) {
        List<SignalAnalyzer> analyzers = new java.util.ArrayList<>();
        analyzers.add(staticAnalyzer);
        List<Double> actualWeights = new java.util.ArrayList<>();
        actualWeights.add(1.0);

        try {
            String key = vault == null ? null : vault.getCredential("GEMINI_API_KEY");
            if (key != null && !key.isBlank()) {
                SignalAnalyzer gemini = buildGemini();
                if (gemini != null && gemini.healthCheck()) {
                    analyzers.add(gemini);
                    actualWeights = weights;
                } else {
                    log.warn("Gemini unhealthy — composite using static-only");
                }
            } else {
                log.warn("No Gemini API key — composite using static-only");
            }
        } catch (Exception e) {
            log.warn("Gemini unavailable: {} — composite using static-only", e.getMessage());
        }
        return new CompositeAnalyzer(analyzers, actualWeights, strategy);
    }

    /** List available configs with their current health. */
    public List<Map<String, Object>> listAvailable() {
        boolean hasGemini = vault != null && vault.hasCredential("GEMINI_API_KEY");
        List<Map<String, Object>> out = new java.util.ArrayList<>();
        out.add(row("static", "Rule-based: RSI, EMA, VWAP, delivery %", false, true));
        out.add(row("gemini", "Google Gemini AI validation", true, hasGemini));
        out.add(row("composite", "Static (70%) + Gemini (30%) weighted", false, true));
        out.add(row("composite_conservative", "Static + Gemini unanimous", false, true));
        out.add(row("composite_ai_primary", "Gemini (70%) + Static (30%)", false, true));
        return out;
    }

    private static Map<String, Object> row(String name, String desc, boolean requiresKey, boolean available) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("description", desc);
        m.put("requires_api_key", requiresKey);
        m.put("available", available);
        return m;
    }

    public void clearCache() {
        // Keep 'static' cached.
        cache.keySet().retainAll(Set.of("static"));
    }
}
