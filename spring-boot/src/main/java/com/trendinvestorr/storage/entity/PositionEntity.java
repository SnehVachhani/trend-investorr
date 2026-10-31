package com.trendinvestorr.storage.entity;

import com.trendinvestorr.core.constants.ProductType;
import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/** Current open position (paper or live). */
@Entity
@Table(
    name = "positions",
    uniqueConstraints = @UniqueConstraint(name = "positions_stock_paper_product_key",
                                          columnNames = {"stock_id", "is_paper", "product_type"})
)
@Getter
@Setter
@NoArgsConstructor
public class PositionEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id")
    private StockEntity stock;

    @Column(name = "quantity", nullable = false)
    private Integer quantity;

    @Column(name = "avg_buy_price", nullable = false, precision = 12, scale = 2)
    private Double avgBuyPrice;

    @Column(name = "current_price", precision = 12, scale = 2)
    private Double currentPrice = 0.0;

    @Column(name = "unrealized_pnl", precision = 14, scale = 2)
    private Double unrealizedPnl = 0.0;

    @Column(name = "is_paper")
    private Boolean isPaper = true;

    @Enumerated(EnumType.STRING)
    @Column(name = "product_type", nullable = false, length = 20)
    private ProductType productType = ProductType.INTRADAY;

    @Column(name = "opened_at", updatable = false, insertable = false)
    private Instant openedAt;

    @UpdateTimestamp
    @Column(name = "updated_at")
    private Instant updatedAt;
}
