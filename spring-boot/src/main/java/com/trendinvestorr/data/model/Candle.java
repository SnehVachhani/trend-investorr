package com.trendinvestorr.data.model;

import com.trendinvestorr.core.constants.CandleInterval;

import java.time.Instant;

/**
 * OHLCV candle at any time interval.
 * Port of {@code data.models.market_data.Candle}.
 *
 * @param symbol        NSE symbol
 * @param timestamp     candle open time (Asia/Kolkata instant)
 * @param open          OHLC open
 * @param high          OHLC high
 * @param low           OHLC low
 * @param close         OHLC close
 * @param volume        traded volume during the interval
 * @param interval      candle time-frame bucket
 * @param deliveryQty   bhavcopy delivery quantity
 * @param deliveryPct   bhavcopy delivery % (0-100)
 * @param vwap          intraday VWAP
 */
public record Candle(
        String symbol,
        Instant timestamp,
        double open,
        double high,
        double low,
        double close,
        long volume,
        CandleInterval interval,
        long deliveryQty,
        double deliveryPct,
        double vwap
) {
    public Candle {
        if (interval == null) interval = CandleInterval.ONE_DAY;
    }

    public Candle(String symbol, Instant ts, double open, double high, double low,
                  double close, long volume, CandleInterval interval) {
        this(symbol, ts, open, high, low, close, volume, interval, 0L, 0.0, 0.0);
    }

    /** Absolute candle body size. */
    public double bodySize() {
        return Math.abs(close - open);
    }

    public boolean isBullish() {
        return close > open;
    }

    public boolean isBearish() {
        return close < open;
    }

    /** High - Low range. */
    public double range() {
        return high - low;
    }

    /** Percentage change from open to close. */
    public double changePct() {
        if (open == 0) return 0.0;
        return ((close - open) / open) * 100.0;
    }
}
