package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.data.model.TechnicalIndicators;
import com.trendinvestorr.engine.indicator.IndicatorMath;
import com.trendinvestorr.storage.entity.AbstractCandleEntity;
import com.trendinvestorr.storage.entity.Candle1dEntity;
import com.trendinvestorr.storage.entity.StockEntity;
import com.trendinvestorr.storage.repository.Candle1dRepository;
import com.trendinvestorr.storage.repository.CandleRepository;
import com.trendinvestorr.storage.repository.StockRepository;
import com.trendinvestorr.storage.repository.TechnicalIndicatorRepository;
import com.trendinvestorr.storage.entity.TechnicalIndicatorEntity;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.time.ZoneId;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Orchestrates technical-indicator computation by fetching candles from
 * {@link CandleRepository}, delegating all formula math to the pure static
 * {@link IndicatorMath}, and caching the resulting {@link TechnicalIndicators}
 * per token for zero-latency hot-path lookup.
 *
 * <p>This is the bridge between the persistence layer and the engine layer:
 * it owns <em>when</em> and <em>where</em> indators get computed, while
 * {@code IndicatorMath} owns the <em>formulas</em> (SRP).
 *
 * <p>Implements {@link IndicatorPort} so indicator-driven scanners depend on
 * the abstraction, not this concrete service (DIP).
 *
 * <p>Port of {@code engine/indicator_engine.py} {@code IndicatorEngine.compute_all}
 * plus the in-memory {@code _indicator_cache} from {@code MomentumScanner}.
 */
@Service
public class IndicatorService implements IndicatorPort {

    private static final Logger log = LoggerFactory.getLogger(IndicatorService.class);

    private final StockRepository stocks;
    private final Candle1dRepository dailyCandles;
    private final CandleRepository candleRepository;
    private final TechnicalIndicatorRepository indicatorRepository;

    /** token → latest indicators. Populated by {@link #warmUp()} / {@link #refresh(String)}. */
    private final Map<String, TechnicalIndicators> cache = new ConcurrentHashMap<>();

    public IndicatorService(StockRepository stocks,
                            Candle1dRepository dailyCandles,
                            CandleRepository candleRepository,
                            TechnicalIndicatorRepository indicatorRepository) {
        this.stocks = stocks;
        this.dailyCandles = dailyCandles;
        this.candleRepository = candleRepository;
        this.indicatorRepository = indicatorRepository;
    }

    // ── IndicatorPort (hot-path lookup) ────────────────────────

    @Override
    public TechnicalIndicators indicatorsForToken(String token) {
        return cache.get(token);
    }

    // ── IndicatorPort (compute + persist) ─────────────────────

    @Override
    public void computeAndSave(long stockId, List<? extends AbstractCandleEntity> daily) {
        if (daily == null || daily.size() < 14) {
            return;
        }
        StockEntity stock = stocks.findById(stockId).orElse(null);
        if (stock == null) return;
        int n = daily.size();
        double[] closes = new double[n];
        double[] highs = new double[n];
        double[] lows = new double[n];
        long[] vols = new long[n];
        double deliveryPct = 0.0;
        for (int i = 0; i < n; i++) {
            AbstractCandleEntity c = daily.get(i);
            closes[i] = c.getClose();
            highs[i] = c.getHigh();
            lows[i] = c.getLow();
            vols[i] = c.getVolume();
            if (c instanceof Candle1dEntity d && d.getDeliveryPct() != null) {
                deliveryPct = Math.max(deliveryPct, d.getDeliveryPct());
            }
        }
        LocalDate date = daily.get(n - 1).getTimestamp().atZone(ZoneId.of("Asia/Kolkata")).toLocalDate();
        TechnicalIndicators ti = compute(closes, highs, lows, vols, deliveryPct, stockId, date);
        upsertEntity(stock, date, ti);
        cache.put(stock.getToken(), ti);
    }

    private void upsertEntity(StockEntity stock, LocalDate date, TechnicalIndicators ti) {
        TechnicalIndicatorEntity row = indicatorRepository
                .findByStockIdAndDate(stock.getId(), date)
                .orElseGet(TechnicalIndicatorEntity::new);
        row.setStock(stock);
        row.setDate(date);
        row.setRsi14(ti.rsi14()); row.setRsi10(ti.rsi10());
        row.setMacdLine(ti.macdLine()); row.setMacdSignal(ti.macdSignal()); row.setMacdHistogram(ti.macdHistogram());
        row.setAdx14(ti.adx14());
        row.setSma20(ti.sma20()); row.setSma50(ti.sma50()); row.setSma200(ti.sma200());
        row.setEma20(ti.ema20()); row.setEma50(ti.ema50());
        row.setSupertrend(ti.supertrend()); row.setSupertrendDirection(ti.supertrendDirection());
        row.setAtr14(ti.atr14()); row.setBollingerUpper(ti.bollingerUpper());
        row.setBollingerLower(ti.bollingerLower()); row.setBollingerWidth(ti.bollingerWidth());
        row.setObv(ti.obv()); row.setVwapDaily(ti.vwapDaily()); row.setDeliveryPct(ti.deliveryPct());
        row.setPivotPoint(ti.pivotPoint()); row.setPivotR1(ti.pivotR1()); row.setPivotR2(ti.pivotR2());
        row.setPivotS1(ti.pivotS1()); row.setPivotS2(ti.pivotS2());
        row.setSupportLevel(ti.supportLevel()); row.setResistanceLevel(ti.resistanceLevel());
        row.setHigh52w(ti.high52w()); row.setLow52w(ti.low52w());
        indicatorRepository.save(row);
    }

    // ── Warm-up & refresh ─────────────────────────────────────

    /**
     * Batch-compute indicators for every active stock. Called by the scheduler
     * during the 8:45 AM pre-market warm-up. Port of
     * {@code main.py} Step 6 indicator warm-up.
     */
    public void warmUp() {
        cache.clear();
        for (StockEntity s : stocks.findByIsActiveTrue()) {
            TechnicalIndicators ti = computeForStock(s);
            if (ti != null) cache.put(s.getToken(), ti);
        }
        log.info("IndicatorService: warmed up indicators for {} stocks", cache.size());
    }

    /** Recompute & cache indicators for a single token (used mid-day after new daily candle). */
    public TechnicalIndicators refresh(String token) {
        StockEntity s = stocks.findByToken(token).orElse(null);
        if (s == null) return null;
        TechnicalIndicators ti = computeForStock(s);
        if (ti != null) cache.put(token, ti);
        return ti;
    }

    /** Drop the whole cache (start of new day). */
    public void reset() {
        cache.clear();
    }

    // ── Computation core ──────────────────────────────────────
    // Converts the CandleRepository daily slice → primitive arrays → IndicatorMath
    // → TechnicalIndicators. Mirrors IndicatorEngine.compute_all threshold-for-threshold.

    private TechnicalIndicators computeForStock(StockEntity stock) {
        List<? extends AbstractCandleEntity> rows =
                candleRepository.findRecent(CandleInterval.ONE_DAY, stock.getId(), 252);
        if (rows == null || rows.size() < 14) return null;

        double[] closes = new double[rows.size()];
        double[] highs  = new double[rows.size()];
        double[] lows   = new double[rows.size()];
        long[]   vols   = new long[rows.size()];
        double deliveryPct = 0.0;
        for (int i = 0; i < rows.size(); i++) {
            AbstractCandleEntity c = rows.get(i);
            closes[i] = c.getClose();
            highs[i]  = c.getHigh();
            lows[i]   = c.getLow();
            vols[i]   = c.getVolume();
            if (c instanceof Candle1dEntity d && d.getDeliveryPct() != null) {
                deliveryPct = Math.max(deliveryPct, d.getDeliveryPct());
            }
        }
        LocalDate date = rows.get(rows.size() - 1).getTimestamp()
                .atZone(ZoneId.of("Asia/Kolkata")).toLocalDate();

        return compute(closes, highs, lows, vols, deliveryPct, stock.getId(), date);
    }

    /** Pure transformation — public so it can be unit-tested without DB. */
    public static TechnicalIndicators compute(double[] closes, double[] highs, double[] lows,
                                              long[] volumes, double deliveryPct,
                                              long stockId, LocalDate date) {
        TechnicalIndicators ti = new TechnicalIndicators(stockId, date);
        int n = closes.length;
        if (n < 14) return ti;

        try {
            // ── Trend ──
            if (n >= 20) ti = withSma20(ti, round(IndicatorMath.sma(closes, 20)));
            if (n >= 50) ti = withSma50(ti, round(IndicatorMath.sma(closes, 50)));
            if (n >= 200) ti = withSma200(ti, round(IndicatorMath.sma(closes, 200)));
            if (n >= 20) ti = withEma20(ti, round(IndicatorMath.ema(closes, 20)));
            if (n >= 50) ti = withEma50(ti, round(IndicatorMath.ema(closes, 50)));

            if (n >= 15) {
                double[] st = IndicatorMath.supertrend(highs, lows, closes, 10, 3.0);
                ti = withSupertrend(ti, round(st[0]), (int) st[1]);
            }

            // ── Momentum ──
            if (n >= 15) {
                ti = withRsi14(ti, round(IndicatorMath.rsi(closes, 14)));
                ti = withRsi10(ti, round(IndicatorMath.rsi(closes, 10)));
            }
            if (n >= 35) {
                double[] m = IndicatorMath.macd(closes, 12, 26, 9);
                ti = withMacd(ti, round(m[0]), round(m[1]), round4(m[2]));
            }
            if (n >= 28) ti = withAdx14(ti, round(IndicatorMath.adx(highs, lows, closes, 14)));

            // ── Volatility ──
            double atr = IndicatorMath.atr(highs, lows, closes, 14);
            ti = withAtr14(ti, round(atr));
            if (n >= 20) {
                double[] bb = IndicatorMath.bollinger(closes, 20, 2.0);
                ti = withBollinger(ti, round(bb[0]), round(bb[2]),
                        bb[1] == 0 ? 0.0 : round4((bb[0] - bb[2]) / bb[1]));
            }

            // ── Volume ──
            ti = withObv(ti, IndicatorMath.obv(closes, volumes));
            ti = withVwap(ti, round(IndicatorMath.vwap(highs, lows, closes, volumes)));
            if (deliveryPct > 0) ti = withDeliveryPct(ti, round(deliveryPct));

            // ── Structure ──
            double prevHigh = highs[n - 1], prevLow = lows[n - 1], prevClose = closes[n - 1];
            double[] piv = IndicatorMath.pivots(prevHigh, prevLow, prevClose);
            ti = withPivots(ti, round(piv[0]), round(piv[1]), round(piv[2]), round(piv[3]), round(piv[4]));

            if (n >= 20) {
                double[] sr = IndicatorMath.supportResistance(highs, lows, 20);
                ti = withSupportResistance(ti, round(sr[0]), round(sr[1]), round(sr[2]), round(sr[3]));
            }
        } catch (Exception e) {
            log.error("Indicator computation error for stockId={}: {}", stockId, e.getMessage());
        }
        return ti;
    }

    // ── with-* record rebuild helpers (TechnicalIndicators is a record) ──

    private static TechnicalIndicators withSma20(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), v, t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withSma50(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), v, t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withSma200(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), v,
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withEma20(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                v, t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withEma50(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), v, t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withSupertrend(TechnicalIndicators t, double v, int dir) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), v, dir,
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withRsi14(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                v, t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withRsi10(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), v, t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withMacd(TechnicalIndicators t, double line, double sig, double hist) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), line, sig, hist, t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withAdx14(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), v,
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withAtr14(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                v, t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withBollinger(TechnicalIndicators t, double upper, double lower, double width) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), upper, lower, width,
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withObv(TechnicalIndicators t, long v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                v, t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withVwap(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), v, t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withDeliveryPct(TechnicalIndicators t, double v) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), v,
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withPivots(TechnicalIndicators t, double pp, double r1, double r2, double s1, double s2) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                pp, r1, r2, s1, s2,
                t.supportLevel(), t.resistanceLevel(), t.high52w(), t.low52w());
    }
    private static TechnicalIndicators withSupportResistance(TechnicalIndicators t, double sup, double res, double h52, double l52) {
        return new TechnicalIndicators(t.stockId(), t.date(), t.sma20(), t.sma50(), t.sma200(),
                t.ema20(), t.ema50(), t.supertrend(), t.supertrendDirection(),
                t.rsi14(), t.rsi10(), t.macdLine(), t.macdSignal(), t.macdHistogram(), t.adx14(),
                t.atr14(), t.bollingerUpper(), t.bollingerLower(), t.bollingerWidth(),
                t.obv(), t.vwapDaily(), t.deliveryPct(),
                t.pivotPoint(), t.pivotR1(), t.pivotR2(), t.pivotS1(), t.pivotS2(),
                sup, res, h52, l52);
    }

    private static double round(double v) {
        if (Double.isNaN(v)) return 0.0;
        return Math.round(v * 100.0) / 100.0;
    }
    private static double round4(double v) {
        if (Double.isNaN(v)) return 0.0;
        return Math.round(v * 10000.0) / 10000.0;
    }
}
