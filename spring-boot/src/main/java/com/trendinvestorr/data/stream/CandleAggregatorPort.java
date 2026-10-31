package com.trendinvestorr.data.stream;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.data.model.Candle;
import com.trendinvestorr.data.model.Tick;

/**
 * Tick → candle aggregation port — abstracts the real-time OHLCV bucket builder.
 *
 * <p>Port of {@code data.streams.candle_aggregator.CandleAggregator}. The boundary keeps
 * scanner code free of aggregation timing concerns: callers feed ticks and flush when
 * they want candles persisted.
 */
public interface CandleAggregatorPort {

    void onTick(Tick tick);

    /** Finalize and persist every open bucket; returns candles flushed. */
    int flushAll();

    void setIntervalMinutes(int minutes);

    CandleInterval interval();

    /** Map SmartAPI token → stock_id for persistence. */
    void setTokenMapping(java.util.Map<String, Long> tokenToStockId);

    /** Clear all buckets (startup / midnight reset). */
    void resetDaily();

    int activeBuckets();

    int pendingCandles();

    default List<Candle> snapshot() {
        return List.of();
    }
}
