package com.trendinvestorr.data.model;

import java.time.LocalDate;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Computed technical indicators for a single stock on a single date.
 * Populated by {@link com.trendinvestorr.engine.indicator.IndicatorEngine}, stored in the
 * {@code technical_indicators} table, and loaded into memory at the 9:00 AM scanner warm-up
 * for zero-latency hot-path access.
 *
 * Port of {@code data.models.market_data.TechnicalIndicators}.
 */
public record TechnicalIndicators(
        long stockId,
        LocalDate date,

        // ── Trend ──
        double sma20,
        double sma50,
        double sma200,
        double ema20,
        double ema50,
        double supertrend,
        int supertrendDirection,    // 1 = bullish, -1 = bearish

        // ── Momentum ──
        double rsi14,
        double rsi10,
        double macdLine,
        double macdSignal,
        double macdHistogram,
        double adx14,

        // ── Volatility ──
        double atr14,
        double bollingerUpper,
        double bollingerLower,
        double bollingerWidth,

        // ── Volume ──
        long obv,
        double vwapDaily,
        double deliveryPct,

        // ── Price Structure ──
        double pivotPoint,
        double pivotR1,
        double pivotR2,
        double pivotS1,
        double pivotS2,
        double supportLevel,
        double resistanceLevel,
        double high52w,
        double low52w
) {
    public TechnicalIndicators(long stockId, LocalDate date) {
        this(stockId, date,
             0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0,
             50.0, 50.0, 0.0, 0.0, 0.0, 0.0,
             0.0, 0.0, 0.0, 0.0,
             0L, 0.0, 0.0,
             0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0);
    }

    public boolean isBullishTrend() { return supertrendDirection == 1; }
    public boolean isOverbought()   { return rsi14 > 70; }
    public boolean isOversold()     { return rsi14 < 30; }
    public boolean isTrending()     { return adx14 > 25.0; }
    public boolean macdBullish()    { return macdHistogram > 0; }

    /** Serialize to a map for signal metadata or DB JSON storage. */
    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("sma_20", sma20); m.put("sma_50", sma50); m.put("sma_200", sma200);
        m.put("ema_20", ema20); m.put("ema_50", ema50);
        m.put("supertrend", supertrend); m.put("supertrend_direction", supertrendDirection);
        m.put("rsi_14", rsi14);
        m.put("macd_line", macdLine); m.put("macd_signal", macdSignal);
        m.put("macd_histogram", macdHistogram); m.put("adx_14", adx14);
        m.put("atr_14", atr14);
        m.put("bollinger_upper", bollingerUpper); m.put("bollinger_lower", bollingerLower);
        m.put("bollinger_width", bollingerWidth);
        m.put("obv", obv); m.put("vwap_daily", vwapDaily); m.put("delivery_pct", deliveryPct);
        m.put("pivot_point", pivotPoint);
        m.put("pivot_r1", pivotR1); m.put("pivot_r2", pivotR2);
        m.put("pivot_s1", pivotS1); m.put("pivot_s2", pivotS2);
        m.put("support_level", supportLevel); m.put("resistance_level", resistanceLevel);
        m.put("high_52w", high52w); m.put("low_52w", low52w);
        return m;
    }
}
