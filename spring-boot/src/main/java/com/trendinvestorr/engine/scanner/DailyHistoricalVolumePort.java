package com.trendinvestorr.engine.scanner;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.storage.entity.AbstractCandleEntity;
import com.trendinvestorr.storage.entity.StockEntity;
import com.trendinvestorr.storage.repository.Candle1dRepository;
import com.trendinvestorr.storage.repository.CandleRepository;
import com.trendinvestorr.storage.repository.StockRepository;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * DB-backed {@link HistoricalVolumePort}. Computes {@code (avg_30d, avg_90d)}
 * from the daily candle table for a given token.
 *
 * <p>Single responsibility: turn a token into historical volume averages. The
 * {@link VolumeScanner} depends only on the {@link HistoricalVolumePort}
 * abstraction (DIP) — this {@code @Component} is the production wiring, but a
 * test can swap in an in-memory stub.
 *
 * <p>Port of the {@code set_historical_volumes(...)} pre-load in
 * {@code main.py} Step 6 that fed the Python {@code VolumeScanner}.
 */
@Component
public class DailyHistoricalVolumePort implements HistoricalVolumePort {

    private final StockRepository stocks;
    private final Candle1dRepository dailyCandles;
    private final CandleRepository candleRepository;

    public DailyHistoricalVolumePort(StockRepository stocks,
                                     Candle1dRepository dailyCandles,
                                     CandleRepository candleRepository) {
        this.stocks = stocks;
        this.dailyCandles = dailyCandles;
        this.candleRepository = candleRepository;
    }

    @Override
    public long[] averagesForToken(String token) {
        StockEntity s = stocks.findByToken(token).orElse(null);
        if (s == null) return null;

        long avg30 = averageOfLast(s.getId(), 30);
        long avg90 = averageOfLast(s.getId(), 90);
        if (avg30 <= 0) return null;
        return new long[]{avg30, avg90};
    }

    private long averageOfLast(Long stockId, int n) {
        // Prefer the typed query if it returns a usable value; otherwise fall back
        // to reading the recent slice from the routing repository and averaging in JVM.
        Long avg = dailyCandles.averageVolumeTopN(stockId, n);
        if (avg != null && avg > 0) return avg;

        List<? extends AbstractCandleEntity> rows =
                candleRepository.findRecent(CandleInterval.ONE_DAY, stockId, n);
        if (rows == null || rows.isEmpty()) return 0L;
        long sum = 0;
        int count = 0;
        for (AbstractCandleEntity r : rows) {
            if (r.getVolume() != null && r.getVolume() > 0) {
                sum += r.getVolume();
                count++;
            }
        }
        return count == 0 ? 0L : sum / count;
    }
}
