package com.trendinvestorr.analyzer;

import com.trendinvestorr.core.constants.AnalysisAction;
import com.trendinvestorr.core.constants.SignalType;
import com.trendinvestorr.data.model.AnalysisResult;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.data.model.Signal;
import com.trendinvestorr.data.model.TechnicalIndicators;
import com.trendinvestorr.engine.indicator.IndicatorMath;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Pure rule-based signal validation using classic technical indicators
 * (RSI, EMA20/50, VWAP, delivery %, volume). No AI, no API keys —
 * the default analyzer that works offline.
 *
 * <p><b>SOLID:</b> SRP — only rule evaluation; indicator math delegates to
 * {@link IndicatorMath}. OCP — additional pre-computed-indicator bonus rules
 * are appended without rewriting the base scoring.
 *
 * <p>Port of {@code analyzers/static_rule_analyzer.py}.
 */
@Service
public class StaticRuleAnalyzer implements SignalAnalyzer {

    private static final Logger log = LoggerFactory.getLogger(StaticRuleAnalyzer.class);

    static final int RSI_PERIOD = 14;
    static final int EMA_SHORT = 20;
    static final int EMA_LONG = 50;
    static final double RSI_OVERBOUGHT = 70.0;
    static final double RSI_OVERSOLD = 30.0;
    static final int MIN_CANDLES = 50;
    static final double MIN_DELIVERY_PCT = 30.0;

    @Override
    public String name() { return "static"; }

    @Override
    public boolean healthCheck() { return true; }  // no external deps

    @Override
    public AnalysisResult analyze(Signal signal, List<Candle> candles) {
        try {
            TechnicalIndicators precomputed = precomputedFrom(signal);
            IndicatorPack pack;
            if (precomputed != null) {
                pack = fromPrecomputed(signal, precomputed);
            } else {
                pack = fromCandles(signal, candles);
                if (pack == null) {
                    return AnalysisResult.skip(name(),
                            "Insufficient data: " + (candles == null ? 0 : candles.size())
                                    + " candles (need " + MIN_CANDLES + ")");
                }
            }

            if (signal.signalType() == SignalType.BUY) {
                return evaluateBuy(signal, pack, precomputed);
            }
            return evaluateSell(signal, pack);
        } catch (Exception e) {
            log.error("Static analyzer error for {}: {}", signal.symbol(), e.getMessage());
            return AnalysisResult.skip(name(), "Analysis error: " + e.getMessage());
        }
    }

    // ── Indicator extraction (fast path vs slow path) ────────

    @SuppressWarnings("unchecked")
    private static TechnicalIndicators precomputedFrom(Signal signal) {
        Object o = signal.metadata() == null ? null : signal.metadata().get("precomputed_indicators");
        return (o instanceof TechnicalIndicators ti) ? ti : null;
    }

    private static IndicatorPack fromPrecomputed(Signal signal, TechnicalIndicators ti) {
        double price = signal.price();
        double rsi = ti.rsi14();
        double emaShort = ti.ema20() > 0 ? ti.ema20() : price;
        double emaLong = ti.ema50() > 0 ? ti.ema50() : price;
        double vwap = ti.vwapDaily() > 0 ? ti.vwapDaily() : price;
        Map<String, Object> ind = new LinkedHashMap<>(ti.toMap());
        ind.put("current_price", round(price));
        ind.put("volume_ratio", round(signal.volumeRatio()));
        ind.put("rsi", round(rsi));
        ind.put("ema_short", round(emaShort));
        ind.put("ema_long", round(emaLong));
        ind.put("vwap_proxy", round(vwap));
        ind.put("delivery_pct", round(ti.deliveryPct()));
        return new IndicatorPack(price, rsi, emaShort, emaLong, vwap, ti.deliveryPct(), ind);
    }

    private static IndicatorPack fromCandles(Signal signal, List<Candle> candles) {
        if (candles == null || candles.size() < MIN_CANDLES) return null;
        int n = candles.size();
        double[] closes = new double[n];
        double[] highs = new double[n];
        double[] lows = new double[n];
        long[] vols = new long[n];
        for (int i = 0; i < n; i++) {
            Candle c = candles.get(i);
            closes[i] = c.close(); highs[i] = c.high(); lows[i] = c.low(); vols[i] = c.volume();
        }
        double price = closes[n - 1];
        double rsi = IndicatorMath.rsi(closes, RSI_PERIOD);
        double emaShort = IndicatorMath.ema(closes, EMA_SHORT);
        double emaLong = IndicatorMath.ema(closes, EMA_LONG);
        double vwap = IndicatorMath.vwap(highs, lows, closes, vols);
        double deliveryPct = signal.metadata() == null
                ? 0.0 : ((Number) signal.metadata().getOrDefault("delivery_pct", 0.0)).doubleValue();

        Map<String, Object> ind = new LinkedHashMap<>();
        ind.put("rsi", round(rsi));
        ind.put("ema_short", round(emaShort));
        ind.put("ema_long", round(emaLong));
        ind.put("vwap_proxy", round(vwap));
        ind.put("current_price", round(price));
        ind.put("delivery_pct", round(deliveryPct));
        ind.put("volume_ratio", round(signal.volumeRatio()));
        return new IndicatorPack(price, Double.isNaN(rsi) ? 50.0 : rsi,
                Double.isNaN(emaShort) ? price : emaShort,
                Double.isNaN(emaLong) ? price : emaLong,
                Double.isNaN(vwap) ? price : vwap,
                deliveryPct, ind);
    }

    // ── BUY evaluation ──────────────────────────────────────

    private AnalysisResult evaluateBuy(Signal signal, IndicatorPack p, TechnicalIndicators ti) {
        double confidence = 0.0;
        List<String> reasons = new ArrayList<>();
        List<String> redFlags = new ArrayList<>();

        if (p.rsi < RSI_OVERBOUGHT) {
            confidence += 0.15;
            if (p.rsi < RSI_OVERSOLD) { reasons.add(String.format("RSI oversold (%.0f)", p.rsi)); confidence += 0.05; }
            else if (p.rsi < 50) reasons.add(String.format("RSI neutral-low (%.0f)", p.rsi));
            else reasons.add(String.format("RSI acceptable (%.0f)", p.rsi));
        } else redFlags.add(String.format("RSI overbought (%.0f)", p.rsi));

        if (p.price > p.emaShort && p.emaShort > p.emaLong) { confidence += 0.20; reasons.add("Strong uptrend: price > EMA20 > EMA50"); }
        else if (p.price > p.emaShort) { confidence += 0.10; reasons.add("Above short EMA"); }
        else if (p.price > p.emaLong) { confidence += 0.05; reasons.add("Above long EMA — consolidation"); }
        else redFlags.add("Below both EMAs — downtrend");

        if (p.price > p.vwap) { confidence += 0.10; reasons.add("Above VWAP — buyers in control"); }
        else redFlags.add("Below VWAP — selling pressure");

        if (p.deliveryPct >= MIN_DELIVERY_PCT) { confidence += 0.15; reasons.add(String.format("Delivery %.0f%% — institutional", p.deliveryPct)); }
        else if (p.deliveryPct > 0) { confidence += 0.05; reasons.add(String.format("Delivery %.0f%% — moderate", p.deliveryPct)); }

        double vr = signal.volumeRatio();
        if (vr >= 3.0) { confidence += 0.15; reasons.add(String.format("Strong volume %.1fx", vr)); }
        else if (vr >= 2.0) { confidence += 0.10; reasons.add(String.format("Volume %.1fx", vr)); }
        else confidence += 0.05;

        if (redFlags.isEmpty()) { confidence += 0.15; reasons.add("No red flags — clean setup"); }

        if (ti != null) applyPrecomputedBonus(ti, p.price, confidence, reasons, redFlags);

        confidence = Math.min(confidence, 1.0);
        AnalysisAction action;
        String reasoning;
        if (!redFlags.isEmpty() && redFlags.size() >= 2) {
            action = AnalysisAction.SKIP; reasoning = "SKIP: Multiple red flags — " + String.join("; ", redFlags);
        } else if (confidence >= 0.6) {
            action = AnalysisAction.BUY; reasoning = "BUY: " + String.join(" | ", reasons);
        } else if (confidence >= 0.4) {
            action = AnalysisAction.HOLD; reasoning = "HOLD: mixed — " + joinAll(reasons, redFlags);
        } else {
            action = AnalysisAction.SKIP; reasoning = "SKIP: weak — " + joinAll(reasons, redFlags);
        }
        return new AnalysisResult(action, confidence, reasoning, name(), signal.symbol(), p.indicators, "");
    }

    private void applyPrecomputedBonus(TechnicalIndicators ti, double price,
                                       double confidence, List<String> reasons, List<String> redFlags) {
        if (ti.supertrendDirection() == 1) { confidence += 0.05; reasons.add("Supertrend bullish"); }
        else if (ti.supertrendDirection() == -1) redFlags.add("Supertrend bearish");
        if (ti.adx14() > 25) { confidence += 0.03; reasons.add(String.format("Strong trend (ADX=%.0f)", ti.adx14())); }
        if (ti.macdHistogram() > 0) { confidence += 0.03; reasons.add("MACD bullish"); }
        else if (ti.macdHistogram() < 0) redFlags.add("MACD bearish");
        if (ti.bollingerLower() > 0 && price < ti.bollingerLower()) { confidence += 0.03; reasons.add("Below Bollinger lower — bounce"); }
        if (ti.supportLevel() > 0) {
            double dist = (price - ti.supportLevel()) / price * 100;
            if (dist > 0 && dist < 2) { confidence += 0.05; reasons.add(String.format("Near support %.0f", ti.supportLevel())); }
        }
        if (ti.resistanceLevel() > 0) {
            double dist = (ti.resistanceLevel() - price) / price * 100;
            if (dist > 0 && dist < 1) redFlags.add(String.format("Near resistance %.0f", ti.resistanceLevel()));
        }
    }

    // ── SELL evaluation ──────────────────────────────────────

    private AnalysisResult evaluateSell(Signal signal, IndicatorPack p) {
        double confidence = 0.0;
        List<String> reasons = new ArrayList<>();
        if (p.rsi > RSI_OVERBOUGHT) { confidence += 0.25; reasons.add(String.format("RSI overbought (%.0f)", p.rsi)); }
        if (p.price < p.emaShort && p.emaShort < p.emaLong) { confidence += 0.25; reasons.add("Confirmed downtrend"); }
        else if (p.price < p.emaShort) { confidence += 0.15; reasons.add("Below short EMA"); }
        if (p.price < p.vwap) { confidence += 0.15; reasons.add("Below VWAP — sellers in control"); }
        if (signal.volumeRatio() >= 3.0) { confidence += 0.15; reasons.add(String.format("Heavy distribution %.1fx", signal.volumeRatio())); }
        confidence = Math.min(confidence, 1.0);

        AnalysisAction action; String reasoning;
        if (confidence >= 0.5) { action = AnalysisAction.SELL; reasoning = "SELL: " + String.join(" | ", reasons); }
        else if (confidence >= 0.3) { action = AnalysisAction.HOLD; reasoning = "HOLD: weak sell — " + String.join(" | ", reasons); }
        else { action = AnalysisAction.SKIP; reasoning = "SKIP: No clear sell signal"; }
        return new AnalysisResult(action, confidence, reasoning, name(), signal.symbol(), p.indicators, "");
    }

    // ── helpers ──────────────────────────────────────────────
    private static String joinAll(List<String> a, List<String> b) {
        List<String> all = new ArrayList<>(a); all.addAll(b);
        return String.join(" | ", all);
    }
    private static double round(double v) { return Double.isNaN(v) ? 0.0 : Math.round(v * 100.0) / 100.0; }

    /** Bundle of evaluated indicator values for one signal. */
    private record IndicatorPack(double price, double rsi, double emaShort, double emaLong,
                                 double vwap, double deliveryPct, Map<String, Object> indicators) {}
}
