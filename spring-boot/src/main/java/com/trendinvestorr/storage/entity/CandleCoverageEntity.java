package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/**
 * Tracks what data we have for each stock at each interval. Used by the
 * {@code DataIngestionService.backfill_all_intervals()} loop to decide whether
 * to fetch. Replaces Python {@code CandleCoverageStore}.
 */
@Entity
@Table(
    name = "candle_coverage",
    uniqueConstraints = @UniqueConstraint(name = "candle_coverage_stock_interval_key",
                                          columnNames = {"stock_id", "interval"})
)
@Getter
@Setter
@NoArgsConstructor
public class CandleCoverageEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id", nullable = false)
    private StockEntity stock;

    @Column(name = "interval", nullable = false, length = 20)
    private String interval;

    @Column(name = "earliest_ts", nullable = false)
    private Instant earliestTs;

    @Column(name = "latest_ts", nullable = false)
    private Instant latestTs;

    @Column(name = "candle_count")
    private Integer candleCount = 0;

    @Column(name = "source", length = 30)
    private String source = "angel_one";

    @UpdateTimestamp
    @Column(name = "last_updated")
    private Instant lastUpdated;
}
