package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.time.LocalDate;
import java.time.Instant;

/** Daily P&L snapshot used for the 17:00 summary notification. */
@Entity
@Table(
    name = "daily_pnl",
    uniqueConstraints = @UniqueConstraint(name = "daily_pnl_date_paper_key",
                                          columnNames = {"date", "is_paper"})
)
@Getter
@Setter
@NoArgsConstructor
public class DailyPnlEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "date", nullable = false)
    private LocalDate date;

    @Column(name = "realized_pnl", precision = 14, scale = 2)
    private Double realizedPnl = 0.0;

    @Column(name = "unrealized_pnl", precision = 14, scale = 2)
    private Double unrealizedPnl = 0.0;

    @Column(name = "total_trades")
    private Integer totalTrades = 0;

    @Column(name = "winning_trades")
    private Integer winningTrades = 0;

    @Column(name = "losing_trades")
    private Integer losingTrades = 0;

    @Column(name = "max_drawdown", precision = 14, scale = 2)
    private Double maxDrawdown = 0.0;

    @Column(name = "portfolio_value", precision = 16, scale = 2)
    private Double portfolioValue = 0.0;

    @Column(name = "cash_balance", precision = 16, scale = 2)
    private Double cashBalance = 0.0;

    @Column(name = "is_paper")
    private Boolean isPaper = true;

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;
}
