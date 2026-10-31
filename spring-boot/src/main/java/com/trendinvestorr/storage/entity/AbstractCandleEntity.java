package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.time.Instant;

/**
 * Shared OHLCV columns for every candle interval table.
 * Concrete subclass entities map to {@code candles_1m / 5m / 15m / 1h / 1d / 1w}.
 *
 * <p>Each candle table has identical columns for the intraday intervals; the daily
 * table adds {@code delivery_qty} and {@code delivery_pct} (see {@link Candle1dEntity}).
 */
@MappedSuperclass
@Getter
@Setter
@NoArgsConstructor
public abstract class AbstractCandleEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id", nullable = false)
    private StockEntity stock;

    @Column(name = "timestamp", nullable = false)
    private Instant timestamp;

    @Column(name = "open", nullable = false, precision = 12, scale = 2)
    private Double open;

    @Column(name = "high", nullable = false, precision = 12, scale = 2)
    private Double high;

    @Column(name = "low", nullable = false, precision = 12, scale = 2)
    private Double low;

    @Column(name = "close", nullable = false, precision = 12, scale = 2)
    private Double close;

    @Column(name = "volume", nullable = false)
    private Long volume = 0L;

    @Column(name = "vwap", precision = 12, scale = 2)
    private Double vwap = 0.0;
}
