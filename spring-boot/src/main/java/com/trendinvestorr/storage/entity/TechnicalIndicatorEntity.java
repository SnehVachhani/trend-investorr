package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.time.LocalDate;

/**
 * EOD technical-indicator cache row for one stock on one date.
 * Maps to {@code technical_indicators}. Replaces Python {@code TechnicalIndicatorStore}.
 */
@Entity
@Table(
    name = "technical_indicators",
    uniqueConstraints = @UniqueConstraint(name = "tech_ind_stock_date_key",
                                          columnNames = {"stock_id", "date"})
)
@Getter
@Setter
@NoArgsConstructor
public class TechnicalIndicatorEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id", nullable = false)
    private StockEntity stock;

    @Column(name = "date", nullable = false)
    private LocalDate date;

    // ── Momentum ──
    @Column(name = "rsi_14", precision = 6, scale = 2)
    private Double rsi14 = 50.0;
    @Column(name = "rsi_10", precision = 6, scale = 2)
    private Double rsi10 = 50.0;
    @Column(name = "macd_line", precision = 12, scale = 2)
    private Double macdLine = 0.0;
    @Column(name = "macd_signal", precision = 12, scale = 2)
    private Double macdSignal = 0.0;
    @Column(name = "macd_histogram", precision = 12, scale = 4)
    private Double macdHistogram = 0.0;
    @Column(name = "adx_14", precision = 6, scale = 2)
    private Double adx14 = 0.0;

    // ── Trend ──
    @Column(name = "sma_20", precision = 12, scale = 2)
    private Double sma20 = 0.0;
    @Column(name = "sma_50", precision = 12, scale = 2)
    private Double sma50 = 0.0;
    @Column(name = "sma_200", precision = 12, scale = 2)
    private Double sma200 = 0.0;
    @Column(name = "ema_20", precision = 12, scale = 2)
    private Double ema20 = 0.0;
    @Column(name = "ema_50", precision = 12, scale = 2)
    private Double ema50 = 0.0;
    @Column(name = "supertrend", precision = 12, scale = 2)
    private Double supertrend = 0.0;
    @Column(name = "supertrend_direction")
    private Integer supertrendDirection = 0;

    // ── Volatility ──
    @Column(name = "atr_14", precision = 12, scale = 2)
    private Double atr14 = 0.0;
    @Column(name = "bollinger_upper", precision = 12, scale = 2)
    private Double bollingerUpper = 0.0;
    @Column(name = "bollinger_lower", precision = 12, scale = 2)
    private Double bollingerLower = 0.0;
    @Column(name = "bollinger_width", precision = 8, scale = 4)
    private Double bollingerWidth = 0.0;

    // ── Volume ──
    @Column(name = "obv")
    private Long obv = 0L;
    @Column(name = "vwap_daily", precision = 12, scale = 2)
    private Double vwapDaily = 0.0;
    @Column(name = "delivery_pct", precision = 6, scale = 2)
    private Double deliveryPct = 0.0;

    // ── Price structure ──
    @Column(name = "pivot_point", precision = 12, scale = 2)
    private Double pivotPoint = 0.0;
    @Column(name = "pivot_r1", precision = 12, scale = 2)
    private Double pivotR1 = 0.0;
    @Column(name = "pivot_r2", precision = 12, scale = 2)
    private Double pivotR2 = 0.0;
    @Column(name = "pivot_s1", precision = 12, scale = 2)
    private Double pivotS1 = 0.0;
    @Column(name = "pivot_s2", precision = 12, scale = 2)
    private Double pivotS2 = 0.0;
    @Column(name = "support_level", precision = 12, scale = 2)
    private Double supportLevel = 0.0;
    @Column(name = "resistance_level", precision = 12, scale = 2)
    private Double resistanceLevel = 0.0;
    @Column(name = "high_52w", precision = 12, scale = 2)
    private Double high52w = 0.0;
    @Column(name = "low_52w", precision = 12, scale = 2)
    private Double low52w = 0.0;
}
