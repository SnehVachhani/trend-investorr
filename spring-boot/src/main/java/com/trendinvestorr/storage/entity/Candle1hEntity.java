package com.trendinvestorr.storage.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/** 1-hour candles. */
@Entity
@Table(
    name = "candles_1h",
    uniqueConstraints = @UniqueConstraint(name = "candles_1h_stock_ts_key",
                                          columnNames = {"stock_id", "timestamp"})
)
@Getter
@Setter
@NoArgsConstructor
public class Candle1hEntity extends AbstractCandleEntity {
}
