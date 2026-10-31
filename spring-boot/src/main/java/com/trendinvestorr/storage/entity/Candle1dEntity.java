package com.trendinvestorr.storage.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * Daily candles. Includes delivery columns from the NSE bhavcopy
 * (populated post-market by the BhavcopyFetcher + DataIngestionService).
 */
@Entity
@Table(
    name = "candles_1d",
    uniqueConstraints = @UniqueConstraint(name = "candles_1d_stock_ts_key",
                                          columnNames = {"stock_id", "timestamp"})
)
@Getter
@Setter
@NoArgsConstructor
public class Candle1dEntity extends AbstractCandleEntity {

    @Column(name = "delivery_qty")
    private Long deliveryQty = 0L;

    @Column(name = "delivery_pct", precision = 6, scale = 2)
    private Double deliveryPct = 0.0;
}
