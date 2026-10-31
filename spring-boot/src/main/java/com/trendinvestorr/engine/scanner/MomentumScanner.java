package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.constants.SignalSource;
import com.trendinvestorr.core.constants.SignalStrength;
import com.trendinvestorr.data.model.Observation;
import com.trendinvestorr.data.model.TechnicalIndicators;
import com.trendinvestorr.data.model.Tick;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Momentum-based signal detection.
 *
 * <p>Consumes pre-computed {@link TechnicalIndicators} (via
 * {@link IndicatorPort}) plus live {@link Tick} prices to detect momentum
 * shifts that often <em>precede</em> volume spikes: RSI extremes, Supertrend
 * confirmation, MACD crossover, ADX trend start, EMA pullback.
 *
 * <p>Each pattern is a separate private detection method (SRP/OCP): adding a
 * new pattern means adding a new method + a call in priority order, not
 * rewriting existing logic. Depends on {@link IndicatorPort} abstraction
 * (DIP), not on {@code IndicatorService} directly.
 *
 * <p>Port of {@code engine/momentum_scanner.py} {@code MomentumScanner} +
 * {@code MomentumScanResult}.
 */
@Service
public class MomentumScanner implements Scanner {

    private static final Logger log = LoggerFactory.getLogger(MomentumScanner.class);

    private final IndicatorPort indicators;
    private final SignalCooldown cooldown;

    /** token → previous tick LTP (for cross detection) */
    private final Map<String, Double> prevPrices = new ConcurrentHashMap<>();

    private long signalsDetected = 0L;

    public MomentumScanner(IndicatorPort indicators) {
        this.indicators = indicators;
        this.cooldown = new SignalCooldown(Duration.ofMinutes(60));
    }

    @Override
    public SignalSource source() {
        return SignalSource.MOMENTUM;
    }

    @Override
    public List<Observation> onTick(Tick tick) {
        if (tick == null || tick.token() == null) return List.of();

        TechnicalIndicators ind = indicators.indicatorsForToken(tick.token());
        if (ind == null) return List.of();
        if (cooldown.isOnCooldown(tick.token())) return List.of();

        double prevPrice = prevPrices.getOrDefault(tick.token(), 0.0);
        prevPrices.put(tick.token(), tick.ltp());
        if (prevPrice <= 0) return List.of();

        // Priority order: strongest signals first.
        Observation o = rsiExtreme(tick, ind);
        if (o == null) o = supertrendConfirmed(tick, ind);
        if (o == null) o = macdCrossover(tick, ind);
        if (o == null) o = adxTrend(tick, ind);
        if (o == null) o = emaAlignment(tick, ind, prevPrice);

        if (o == null) return List.of();

        signalsDetected++;
        cooldown.mark(tick.token());
        return List.of(o);
    }

    @Override
    public void resetDaily() {
        prevPrices.clear();
        cooldown.reset();
        signalsDetected = 0L;
        log.info("MomentumScanner reset for new trading day");
    }

    public Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("signals_detected", signalsDetected);
        return m;
    }

    // ── Pattern detectors (each one pattern, SRP) ─────────────

    private Observation rsiExtreme(Tick tick, TechnicalIndicators ind) {
        // Oversold bounce (bullish)
        if (ind.rsi14() < 30 && ind.supportLevel() > 0 && tick.ltp() > ind.supportLevel()) {
            return observation(tick, "rsi_oversold_bounce", "bullish",
                    SignalStrength.STRONG, Map.of("rsi", ind.rsi14(), "support", ind.supportLevel()));
        }
        // Heavily overbought reversal (bearish)
        if (ind.rsi14() > 75 && ind.resistanceLevel() > 0 && tick.ltp() < ind.resistanceLevel()) {
            return observation(tick, "rsi_overbought_reversal", "bearish",
                    SignalStrength.STRONG, Map.of("rsi", ind.rsi14(), "resistance", ind.resistanceLevel()));
        }
        return null;
    }

    private Observation supertrendConfirmed(Tick tick, TechnicalIndicators ind) {
        if (ind.supertrend() <= 0) return null;
        double price = tick.ltp();
        double distancePct = Math.abs(price - ind.supertrend()) / ind.supertrend() * 100.0;
        if (distancePct < 0.5) return null;

        if (ind.supertrendDirection() == 1 && price > ind.supertrend() && ind.macdHistogram() > 0) {
            return observation(tick, "supertrend_bullish_confirmed", "bullish",
                    SignalStrength.MODERATE, stDetails(ind, distancePct));
        }
        if (ind.supertrendDirection() == -1 && price < ind.supertrend() && ind.macdHistogram() < 0) {
            return observation(tick, "supertrend_bearish_confirmed", "bearish",
                    SignalStrength.MODERATE, stDetails(ind, distancePct));
        }
        return null;
    }

    private Map<String, Object> stDetails(TechnicalIndicators ind, double distancePct) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("supertrend", ind.supertrend());
        m.put("direction", ind.supertrendDirection());
        m.put("macd_hist", ind.macdHistogram());
        m.put("distance_pct", Math.round(distancePct * 100.0) / 100.0);
        return m;
    }

    private Observation macdCrossover(Tick tick, TechnicalIndicators ind) {
        if (ind.macdHistogram() == 0 || ind.adx14() < 20) return null;

        if (ind.macdHistogram() > 0 && ind.macdLine() > ind.macdSignal()
                && ind.ema20() > 0 && tick.ltp() > ind.ema20()) {
            return observation(tick, "macd_bullish_crossover", "bullish",
                    SignalStrength.MODERATE, macdDetails(ind));
        }
        if (ind.macdHistogram() < 0 && ind.macdLine() < ind.macdSignal()
                && ind.ema20() > 0 && tick.ltp() < ind.ema20()) {
            return observation(tick, "macd_bearish_crossover", "bearish",
                    SignalStrength.MODERATE, macdDetails(ind));
        }
        return null;
    }

    private Map<String, Object> macdDetails(TechnicalIndicators ind) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("macd_line", ind.macdLine());
        m.put("macd_signal", ind.macdSignal());
        m.put("macd_histogram", ind.macdHistogram());
        m.put("adx", ind.adx14());
        return m;
    }

    private Observation adxTrend(Tick tick, TechnicalIndicators ind) {
        if (ind.adx14() < 30) return null;

        if (ind.supertrendDirection() == 1 && ind.macdHistogram() > 0) {
            return observation(tick, "strong_trend_bullish", "bullish",
                    SignalStrength.STRONG, Map.of("adx", ind.adx14(),
                            "supertrend_dir", ind.supertrendDirection()));
        }
        if (ind.supertrendDirection() == -1 && ind.macdHistogram() < 0) {
            return observation(tick, "strong_trend_bearish", "bearish",
                    SignalStrength.STRONG, Map.of("adx", ind.adx14(),
                            "supertrend_dir", ind.supertrendDirection()));
        }
        return null;
    }

    private Observation emaAlignment(Tick tick, TechnicalIndicators ind, double prevPrice) {
        if (ind.ema20() <= 0 || ind.ema50() <= 0) return null;

        if (ind.ema20() > ind.ema50()
                && prevPrice <= ind.ema20() && tick.ltp() > ind.ema20()) {
            return observation(tick, "ema_pullback_buy", "bullish",
                    SignalStrength.WEAK, Map.of("ema_20", ind.ema20(), "ema_50", ind.ema50()));
        }
        if (ind.ema20() < ind.ema50()
                && prevPrice >= ind.ema20() && tick.ltp() < ind.ema20()) {
            return observation(tick, "ema_pullback_sell", "bearish",
                    SignalStrength.WEAK, Map.of("ema_20", ind.ema20(), "ema_50", ind.ema50()));
        }
        return null;
    }

    private static Observation observation(Tick tick, String pattern, String direction,
                                           SignalStrength strength, Map<String, Object> details) {
        double confidence = switch (strength) {
            case STRONG   -> 0.75;
            case MODERATE -> 0.60;
            case WEAK     -> 0.45;
            case EXTREME  -> 0.85;
        };
        return new Observation(
                tick.symbol(), tick.token(), SignalSource.MOMENTUM,
                direction, confidence, tick.ltp(), pattern, null, details);
    }
}
