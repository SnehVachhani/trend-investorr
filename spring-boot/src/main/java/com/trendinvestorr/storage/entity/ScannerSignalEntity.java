package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import java.time.LocalDate;
import java.time.Instant;
import java.util.Map;

/**
 * Persisted post-market batch scanner result. Algorithms pre-seed their
 * watchlists with these at the morning 9:00 warm-up.
 * Maps to the {@code scanner_signals} table.
 * Replaces Python {@code ScannerSignalStore} rows.
 */
@Entity
@Table(
    name = "scanner_signals",
    uniqueConstraints = @UniqueConstraint(name = "scanner_signals_uniq",
                                          columnNames = {"stock_id", "scan_date", "scanner_name", "pattern"})
)
@Getter
@Setter
@NoArgsConstructor
public class ScannerSignalEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id", nullable = false)
    private StockEntity stock;

    @Column(name = "scan_date", nullable = false)
    private LocalDate scanDate;

    @Column(name = "target_date", nullable = false)
    private LocalDate targetDate;

    @Column(name = "scanner_name", nullable = false, length = 50)
    private String scannerName;

    @Column(name = "pattern", nullable = false, length = 50)
    private String pattern;

    @Column(name = "direction", nullable = false, length = 10)
    private String direction;

    @Column(name = "confidence", nullable = false, precision = 5, scale = 3)
    private Double confidence;

    @Column(name = "price", nullable = false, precision = 12, scale = 2)
    private Double price;

    @Column(name = "strength", nullable = false, length = 20)
    private String strength;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "details", columnDefinition = "jsonb")
    private Map<String, Object> details;

    @Column(name = "status", length = 20)
    private String status = "ACTIVE";

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;
}
