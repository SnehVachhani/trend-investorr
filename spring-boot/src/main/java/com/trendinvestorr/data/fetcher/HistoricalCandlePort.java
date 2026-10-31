package com.trendinvestorr.data.fetcher;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.data.model.Candle;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

/**
 * Historical candle fetcher port — a broker-agnostic abstraction over the SmartAPI
 * {@code getCandleData} REST endpoint. Lives at the data-ingestion boundary so the
 * domain ({@code DataIngestionService}, scanners, backtester) depends on this port,
 * not on a concrete SDK. New brokers (Zerodha, FYers) plug in by implementing this
 * interface (Open–Closed).
 *
 * <p>Port of {@code data.fetchers.historical_fetcher.HistoricalFetcher}.
 */
public interface HistoricalCandlePort {

    /** Fetch {@code days} of {@code interval} candles ending now, oldest-first. */
    default List<Candle> getCandles(String symbol, String token, int days,
                                    CandleInterval interval, Exchange exchange) {
        return getCandlesRange(symbol, token,
                Instant.now().minusSeconds((long) days * 86_400L),
                Instant.now(), interval, exchange);
    }

    List<Candle> getCandlesRange(String symbol, String token,
                                Instant start, Instant end,
                                CandleInterval interval, Exchange exchange);

    default List<Candle> daily(String symbol, String token, int days) {
        return getCandles(symbol, token, days, CandleInterval.ONE_DAY, Exchange.NSE);
    }

    default Optional<Double> latestPrice(String symbol, String token) {
        List<Candle> c = getCandles(symbol, token, 1, CandleInterval.ONE_DAY, Exchange.NSE);
        return c.isEmpty() ? Optional.empty() : Optional.of(c.get(c.size() - 1).close());
    }
}
