package com.trendinvestorr.storage.entity;

import com.trendinvestorr.core.constants.OrderStatus;
import com.trendinvestorr.core.constants.OrderType;
import com.trendinvestorr.core.constants.OrderVariety;
import com.trendinvestorr.core.constants.ProductType;
import com.trendinvestorr.core.constants.TransactionType;
import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/** Broker or paper-trader order record. */
@Entity
@Table(name = "orders")
@Getter
@Setter
@NoArgsConstructor
public class OrderEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "signal_id")
    private SignalEntity signal;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id")
    private StockEntity stock;

    @Enumerated(EnumType.STRING)
    @Column(name = "order_type", nullable = false, length = 20)
    private OrderType orderType;

    @Enumerated(EnumType.STRING)
    @Column(name = "product_type", nullable = false, length = 20)
    private ProductType productType = ProductType.INTRADAY;

    @Enumerated(EnumType.STRING)
    @Column(name = "transaction_type", nullable = false, length = 10)
    private TransactionType transactionType;

    @Column(name = "quantity", nullable = false)
    private Integer quantity;

    @Column(name = "price", nullable = false, precision = 12, scale = 2)
    private Double price;

    @Column(name = "trigger_price", precision = 12, scale = 2)
    private Double triggerPrice = 0.0;

    @Enumerated(EnumType.STRING)
    @Column(name = "status", nullable = false, length = 20)
    private OrderStatus status = OrderStatus.CREATED;

    @Column(name = "is_paper_trade")
    private Boolean isPaperTrade = true;

    @Column(name = "broker_order_id", length = 50)
    private String brokerOrderId = "";

    @Column(name = "filled_qty")
    private Integer filledQty = 0;

    @Column(name = "avg_fill_price", precision = 12, scale = 2)
    private Double avgFillPrice = 0.0;

    @Column(name = "slippage", precision = 6, scale = 4)
    private Double slippage = 0.0;

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at")
    private Instant updatedAt;
}
