package com.trendinvestorr.data.ingestion;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.data.model.Candle;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

/**
 * Data-ingestion orchestration port — façade over backfill, incremental update, daily/weekly
 * aggregation, bhavcopy storage and indicator computation. Mirrors the Python
 * {@code DataIngestionService} public surface so the scheduler and REST controller can drive
 * the pipeline without depending on broker fetchers or storage repositories directly.
 *
 * <p>The impl wires concrete {@code fetcher}, {@code repository} and {@code indicator} beans;
 * every method returns a small stats map (mirrors Python's {@code dict[str,int]} returns) so
 * callers can persist audit detail without composing the contracts themselves.
 */
public interface DataIngestionPort {

    /** On-demand per-stock backfill. */
    Map<String, Integer> backfillStock(long stockId, String symbol, String token, int days,
                                       List<CandleInterval> intervals);

    /** Mass backfill across the active universe for one interval. */
    Map<String, Integer> backfill(int days, CandleInterval interval, String exchange);

    /** Append today's intraday candles to existing stores. */
    Map<String, Integer> incrementalUpdate(CandleInterval interval, String exchange);

    /** Aggregate a single date's intraday candles → daily row. */
    int aggregateDailyForDate(LocalDate target, CandleInterval source, String exchange);

    /** Aggregate daily → weekly for trailing {@code days}. */
    int aggregateWeekly(String exchange);

    /** Persist bhavcopy-derived delivery candles. */
    int storeBhavcopy(List<Candle> candles);

    /** Recompute and persist technical indicators for {@code target} date (null = today). */
    Map<String, Integer> computeAndStoreIndicators(LocalDate target, String exchange);
}
