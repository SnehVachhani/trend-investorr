package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.time.Instant;

/** Raw live tick batched by the WebSocket tick writer. */
@Entity
@Table(name = "live_ticks")
@Getter
@Setter
@NoArgsConstructor
public class LiveTickEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id")
    private StockEntity stock;

    @Column(name = "token", nullable = false, length = 20)
    private String token;

    @Column(name = "timestamp", nullable = false)
    private Instant timestamp;

    @Column(name = "ltp", nullable = false, precision = 12, scale = 2)
    private Double ltp;

    @Column(name = "volume")
    private Long volume = 0L;

    @Column(name = "open", precision = 12, scale = 2)
    private Double open = 0.0;

    @Column(name = "high", precision = 12, scale = 2)
    private Double high = 0.0;

    @Column(name = "low", precision = 12, scale = 2)
    private Double low = 0.0;

    @Column(name = "best_bid", precision = 12, scale = 2)
    private Double bestBid = 0.0;

    @Column(name = "best_ask", precision = 12, scale = 2)
    private Double bestAsk = 0.0;

    @Column(name = "avg_trade_price", precision = 12, scale = 2)
    private Double avgTradePrice = 0.0;
}
