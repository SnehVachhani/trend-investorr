package com.trendinvestorr.storage.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/** Weekly candles — aggregated from daily, kept for lifetime. */
@Entity
@Table(
    name = "candles_1w",
    uniqueConstraints = @UniqueConstraint(name = "candles_1w_stock_ts_key",
                                          columnNames = {"stock_id", "timestamp"})
)
@Getter
@Setter
@NoArgsConstructor
public class Candle1wEntity extends AbstractCandleEntity {
}
