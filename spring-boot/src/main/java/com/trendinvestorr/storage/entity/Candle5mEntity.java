package com.trendinvestorr.storage.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/** 5-minute candles (built live by {@code CandleAggregator} from WebSocket ticks). */
@Entity
@Table(
    name = "candles_5m",
    uniqueConstraints = @UniqueConstraint(name = "candles_5m_stock_ts_key",
                                          columnNames = {"stock_id", "timestamp"})
)
@Getter
@Setter
@NoArgsConstructor
public class Candle5mEntity extends AbstractCandleEntity {
}
