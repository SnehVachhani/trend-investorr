package com.trendinvestorr.storage.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/** 15-minute candles. */
@Entity
@Table(
    name = "candles_15m",
    uniqueConstraints = @UniqueConstraint(name = "candles_15m_stock_ts_key",
                                          columnNames = {"stock_id", "timestamp"})
)
@Getter
@Setter
@NoArgsConstructor
public class Candle15mEntity extends AbstractCandleEntity {
}
