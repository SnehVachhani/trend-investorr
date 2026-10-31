package com.trendinvestorr.storage.entity;

import com.trendinvestorr.core.constants.SignalSource;
import com.trendinvestorr.core.constants.SignalStatus;
import com.trendinvestorr.core.constants.SignalStrength;
import com.trendinvestorr.core.constants.SignalType;
import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import java.time.Instant;
import java.util.Map;

/** A scanner-emitted signal awaiting action / acted-on / expired. */
@Entity
@Table(name = "signals")
@Getter
@Setter
@NoArgsConstructor
public class SignalEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id")
    private StockEntity stock;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "sector_id")
    private SectorEntity sector;

    @Enumerated(EnumType.STRING)
    @Column(name = "signal_type", nullable = false, length = 10)
    private SignalType signalType;

    @Enumerated(EnumType.STRING)
    @Column(name = "strength", nullable = false, length = 20)
    private SignalStrength strength;

    @Column(name = "score", nullable = false, precision = 5, scale = 3)
    private Double score;

    @Column(name = "analyzer_name", length = 50)
    private String analyzerName = "";

    @Column(name = "analyzer_confidence", precision = 5, scale = 3)
    private Double analyzerConfidence = 0.0;

    @Column(name = "analyzer_reasoning", columnDefinition = "TEXT")
    private String analyzerReasoning = "";

    @Column(name = "volume_current")
    private Long volumeCurrent = 0L;

    @Column(name = "volume_avg")
    private Long volumeAvg = 0L;

    @Column(name = "volume_ratio", precision = 8, scale = 2)
    private Double volumeRatio = 0.0;

    @Column(name = "price", nullable = false, precision = 12, scale = 2)
    private Double price;

    @Enumerated(EnumType.STRING)
    @Column(name = "status", nullable = false, length = 20)
    private SignalStatus status = SignalStatus.PENDING;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "metadata", columnDefinition = "jsonb")
    private Map<String, Object> metadata;

    /** Which scanner originated this signal — persisted in the metadata JSON
     *  (the Python schema has no dedicated source column; it rides in JSON).
     *  Transient here for in-memory filtering, never written to a column. */
    @Transient
    private com.trendinvestorr.core.constants.SignalSource source;

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;
}
