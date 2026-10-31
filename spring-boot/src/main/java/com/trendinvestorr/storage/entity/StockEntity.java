package com.trendinvestorr.storage.entity;

import com.trendinvestorr.core.constants.Exchange;
import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/**
 * Master instrument row. Maps to the {@code stocks} table.
 * Port of the Python {@code data.models.market_data.Stock} dataclass + {@code StockStore}.
 */
@Entity
@Table(
    name = "stocks",
    uniqueConstraints = @UniqueConstraint(name = "stocks_symbol_exchange_key",
                                          columnNames = {"symbol", "exchange"})
)
@Getter
@Setter
@NoArgsConstructor
public class StockEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "symbol", nullable = false, length = 30)
    private String symbol;

    @Column(name = "token", nullable = false, length = 20)
    private String token;

    @Column(name = "name", length = 200)
    private String name = "";

    @Enumerated(EnumType.STRING)
    @Column(name = "exchange", nullable = false, length = 10)
    private Exchange exchange = Exchange.NSE;

    @Column(name = "series", length = 10)
    private String series = "EQ";

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "sector_id")
    private SectorEntity sector;

    @Column(name = "industry", length = 200)
    private String industry = "";

    @Column(name = "market_cap_cr", precision = 14, scale = 2)
    private Double marketCapCr = 0.0;

    @Column(name = "lot_size")
    private Integer lotSize = 1;

    @Column(name = "is_active")
    private Boolean isActive = true;

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at")
    private Instant updatedAt;

    /** SmartAPI trading-symbol format, e.g. {@code "RELIANCE-EQ"}. */
    public String tradingSymbol() {
        return symbol + "-" + (series == null ? "EQ" : series);
    }
}
