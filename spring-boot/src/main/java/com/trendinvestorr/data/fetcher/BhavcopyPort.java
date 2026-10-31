package com.trendinvestorr.data.fetcher;

import com.trendinvestorr.data.model.Candle;

import java.time.Instant;
import java.util.List;

/**
 * NSE bhavcopy (EOD trade file) port — abstracts the NSE EOD CSV download + parse pipeline.
 *
 * <p>Single Responsibility: consumer code asks for a day's {@link Candle}s as OHLCV+delivery;
 * the impl owns HTTP, cookie prefetch, zip extraction, column-name normalization and caching.
 *
 * <p>Port of {@code data.fetchers.bhavcopy_fetcher.BhavcopyFetcher}.
 */
public interface BhavcopyPort {

    /** Fetch the bhavcopy for {@code targetDate}; falls back T-1/T-2 on missing data. */
    List<Candle> fetch(Instant targetDate, int maxFallbackDays);

    default List<Candle> fetch() {
        return fetch(Instant.now(), 3);
    }

    /** Top-N delivery-quality candles (delivery % ≥ {@code minPct}, volume ≥ {@code minVol}). */
    List<Candle> deliveryLeaders(Instant targetDate, double minDeliveryPct, long minVolume);

    /** Top-N volume leaders for the trading day. */
    List<Candle> volumeLeaders(Instant targetDate, int topN);

    /** Evict cache entries older than {@code keepDays}. */
    int cleanupOldCache(int keepDays);
}
