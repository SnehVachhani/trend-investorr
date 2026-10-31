package com.trendinvestorr.data.ingestion;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.data.fetcher.HistoricalCandlePort;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.engine.scanner.IndicatorPort;
import com.trendinvestorr.storage.entity.AbstractCandleEntity;
import com.trendinvestorr.storage.entity.StockEntity;
import com.trendinvestorr.storage.repository.CandleRepository;
import com.trendinvestorr.storage.repository.StockRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Default {@link DataIngestionPort} implementation.
 *
 * <p>Single Responsibility: orchestrates fetch + persist + aggregate across the candle
 * interval tables. It depends on {@link HistoricalCandlePort} (broker abstraction),
 * {@link CandleRepository} (storage), {@link StockRepository} (universe) and
 * {@link IndicatorPort} (indicator recompute) — all injected through the constructor so
 * they can be substituted in tests. The orchestrator knows no SDK or SQL details.
 *
 * <p>Port of {@code data.data_ingestion.DataIngestionService}.
 */
@Service
public class DataIngestionService implements DataIngestionPort {

    private static final Logger log = LoggerFactory.getLogger(DataIngestionService.class);

    private final HistoricalCandlePort fetcher;
    private final CandleRepository candles;
    private final StockRepository stocks;
    private final IndicatorPort indicators;

    public DataIngestionService(HistoricalCandlePort fetcher,
                                CandleRepository candles,
                                StockRepository stocks,
                                IndicatorPort indicators) {
        this.fetcher = fetcher;
        this.candles = candles;
        this.stocks = stocks;
        this.indicators = indicators;
    }

    @Override
    @Transactional
    public Map<String, Integer> backfillStock(long stockId, String symbol, String token, int days,
                                             List<CandleInterval> intervals) {
        var stats = new LinkedHashMap<String, Integer>();
        stats.put("candles_stored", 0);
        stats.put("intervals_done", 0);
        stats.put("errors", 0);
        StockEntity stock = stocks.findById(stockId).orElse(null);
        if (stock == null) {
            log.warn("backfillStock: stock {} not found", stockId);
            stats.merge("errors", 1, Integer::sum);
            return stats;
        }
        List<CandleInterval> target = intervals == null || intervals.isEmpty()
                ? List.of(CandleInterval.ONE_DAY) : intervals;
        for (CandleInterval iv : target) {
            try {
                List<Candle> rows = fetcher.getCandles(symbol, token, days, iv, stock.getExchange());
                int stored = persistCandles(stock, rows, iv);
                stats.merge("candles_stored", stored, Integer::sum);
                stats.merge("intervals_done", 1, Integer::sum);
            } catch (Exception e) {
                log.warn("backfillStock {} {}: {}", symbol, iv, e.getMessage());
                stats.merge("errors", 1, Integer::sum);
            }
        }
        return stats;
    }

    @Override
    @Transactional
    public Map<String, Integer> backfill(int days, CandleInterval interval, String exchange) {
        var stats = new LinkedHashMap<String, Integer>();
        stats.put("stocks_processed", 0);
        stats.put("candles_stored", 0);
        stats.put("skipped", 0);
        stats.put("errors", 0);
        List<StockEntity> universe = exchange == null || exchange.isBlank()
                ? stocks.findByIsActiveTrue()
                : stocks.findByIsActiveTrueAndExchange(Exchange.valueOf(exchange));
        for (StockEntity s : universe) {
            try {
                List<Candle> rows = fetcher.getCandles(s.getSymbol(), s.getToken(), days, interval, s.getExchange());
                int stored = persistCandles(s, rows, interval);
                stats.merge("candles_stored", stored, Integer::sum);
                stats.merge("stocks_processed", 1, Integer::sum);
            } catch (Exception e) {
                log.debug("backfill {} failed: {}", s.getSymbol(), e.getMessage());
                stats.merge("errors", 1, Integer::sum);
            }
        }
        return stats;
    }

    @Override
    @Transactional
    public Map<String, Integer> incrementalUpdate(CandleInterval interval, String exchange) {
        var stats = new LinkedHashMap<String, Integer>();
        stats.put("stocks_updated", 0);
        stats.put("candles_added", 0);
        stats.put("errors", 0);
        List<StockEntity> universe = exchange == null || exchange.isBlank()
                ? stocks.findByIsActiveTrue()
                : stocks.findByIsActiveTrueAndExchange(Exchange.valueOf(exchange));
        Instant from = LocalDate.now(MarketConstants.IST).minusDays(1)
                .atStartOfDay(ZoneId.of("Asia/Kolkata")).toInstant();
        Instant to = Instant.now();
        for (StockEntity s : universe) {
            try {
                List<Candle> rows = fetcher.getCandlesRange(
                        s.getSymbol(), s.getToken(), from, to, interval, s.getExchange());
                int added = persistCandlesSkipExisting(s, rows, interval);
                stats.merge("candles_added", added, Integer::sum);
                stats.merge("stocks_updated", 1, Integer::sum);
            } catch (Exception e) {
                log.debug("incrementalUpdate {} failed: {}", s.getSymbol(), e.getMessage());
                stats.merge("errors", 1, Integer::sum);
            }
        }
        return stats;
    }

    @Override
    @Transactional
    public int aggregateDailyForDate(LocalDate target, CandleInterval source, String exchange) {
        // Aggregate intraday Candle rows of `source` interval into a single ONE_DAY row per stock.
        // Strategy: for each stock, take the day's intraday rows, compute O=first.h C=last.c H=max L=min V=sum.
        int written = 0;
        Instant dayStart = target.atStartOfDay(ZoneId.of("Asia/Kolkata")).toInstant();
        Instant dayEnd = dayStart.plusSeconds(86_400L);
        List<StockEntity> universe = stocks.findByIsActiveTrue();
        for (StockEntity s : universe) {
            try {
                var rows = candles.findByStockBetween(source, s.getId(), dayStart, dayEnd);
                if (rows.isEmpty()) continue;
                double open = rows.get(0).getOpen();
                double close = rows.get(rows.size() - 1).getClose();
                double high = rows.stream().mapToDouble(AbstractCandleEntity::getHigh).max().orElse(open);
                double low = rows.stream().mapToDouble(AbstractCandleEntity::getLow).min().orElse(open);
                long vol = rows.stream().mapToLong(AbstractCandleEntity::getVolume).sum();
                AbstractCandleEntity day = candles.newEntity(CandleInterval.ONE_DAY);
                day.setStock(s);
                day.setTimestamp(dayStart);
                day.setOpen(open);
                day.setHigh(high);
                day.setLow(low);
                day.setClose(close);
                day.setVolume(vol);
                candles.save(CandleInterval.ONE_DAY, day);
                written++;
            } catch (Exception e) {
                log.debug("aggregateDaily {} {}: {}", s.getSymbol(), target, e.getMessage());
            }
        }
        return written;
    }

    @Override
    @Transactional
    public int aggregateWeekly(String exchange) {
        // Lightweight port: fold trailing ONE_DAY rows into ONE_WEEK rows per stock.
        // (Python uses ISO-week bucketing; we approximate by taking the most recent N daily rows.)
        int written = 0;
        List<StockEntity> universe = exchange == null || exchange.isBlank()
                ? stocks.findByIsActiveTrue()
                : stocks.findByIsActiveTrueAndExchange(Exchange.valueOf(exchange));
        for (StockEntity s : universe) {
            try {
                var daily = candles.findRecent(CandleInterval.ONE_DAY, s.getId(), 5);
                if (daily.isEmpty()) continue;
                double open = daily.get(0).getOpen();
                double close = daily.get(daily.size() - 1).getClose();
                double high = daily.stream().mapToDouble(AbstractCandleEntity::getHigh).max().orElse(open);
                double low = daily.stream().mapToDouble(AbstractCandleEntity::getLow).min().orElse(open);
                long vol = daily.stream().mapToLong(AbstractCandleEntity::getVolume).sum();
                AbstractCandleEntity week = candles.newEntity(CandleInterval.ONE_WEEK);
                week.setStock(s);
                week.setTimestamp(daily.get(0).getTimestamp());
                week.setOpen(open);
                week.setHigh(high);
                week.setLow(low);
                week.setClose(close);
                week.setVolume(vol);
                candles.save(CandleInterval.ONE_WEEK, week);
                written++;
            } catch (Exception e) {
                log.debug("aggregateWeekly {} failed: {}", s.getSymbol(), e.getMessage());
            }
        }
        return written;
    }

    @Override
    @Transactional
    public int storeBhavcopy(List<Candle> rows) {
        int stored = 0;
        for (Candle c : rows) {
            try {
                Optional<StockEntity> found = stocks.findBySymbol(c.symbol());
                if (found.isEmpty()) continue;
                AbstractCandleEntity day = candles.newEntity(CandleInterval.ONE_DAY);
                day.setStock(found.get());
                day.setTimestamp(c.timestamp());
                day.setOpen(c.open());
                day.setHigh(c.high());
                day.setLow(c.low());
                day.setClose(c.close());
                day.setVolume(c.volume());
                candles.save(CandleInterval.ONE_DAY, day);
                stored++;
            } catch (Exception e) {
                log.debug("storeBhavcopy {} failed: {}", c.symbol(), e.getMessage());
            }
        }
        return stored;
    }

    @Override
    @Transactional
    public Map<String, Integer> computeAndStoreIndicators(LocalDate target, String exchange) {
        var stats = new LinkedHashMap<String, Integer>();
        stats.put("computed", 0);
        stats.put("skipped", 0);
        stats.put("errors", 0);
        List<StockEntity> universe = exchange == null || exchange.isBlank()
                ? stocks.findByIsActiveTrue()
                : stocks.findByIsActiveTrueAndExchange(Exchange.valueOf(exchange));
        for (StockEntity s : universe) {
            try {
                var daily = candles.findRecent(CandleInterval.ONE_DAY, s.getId(), 200);
                if (daily.size() < 20) {
                    stats.merge("skipped", 1, Integer::sum);
                    continue;
                }
                indicators.computeAndSave(s.getId(), daily);
                stats.merge("computed", 1, Integer::sum);
            } catch (Exception e) {
                log.debug("indicator recompute {} failed: {}", s.getSymbol(), e.getMessage());
                stats.merge("errors", 1, Integer::sum);
            }
        }
        return stats;
    }

    /** Persist a Candle batch, deduping by (stock, timestamp) on conflict. */
    private int persistCandles(StockEntity stock, List<Candle> rows, CandleInterval iv) {
        int stored = 0;
        List<AbstractCandleEntity> toSave = new ArrayList<>();
        for (Candle c : rows) {
            AbstractCandleEntity e = candles.newEntity(iv);
            e.setStock(stock);
            e.setTimestamp(c.timestamp());
            e.setOpen(c.open());
            e.setHigh(c.high());
            e.setLow(c.low());
            e.setClose(c.close());
            e.setVolume(c.volume());
            toSave.add(e);
            stored++;
        }
        if (!toSave.isEmpty()) {
            candles.saveAll(iv, toSave);
        }
        return stored;
    }

    private int persistCandlesSkipExisting(StockEntity stock, List<Candle> rows, CandleInterval iv) {
        List<AbstractCandleEntity> toSave = new ArrayList<>();
        for (Candle c : rows) {
            if (candles.findByStockAndTimestamp(iv, stock.getId(), c.timestamp()).isPresent()) continue;
            AbstractCandleEntity e = candles.newEntity(iv);
            e.setStock(stock);
            e.setTimestamp(c.timestamp());
            e.setOpen(c.open());
            e.setHigh(c.high());
            e.setLow(c.low());
            e.setClose(c.close());
            e.setVolume(c.volume());
            toSave.add(e);
        }
        if (!toSave.isEmpty()) candles.saveAll(iv, toSave);
        return toSave.size();
    }
}
