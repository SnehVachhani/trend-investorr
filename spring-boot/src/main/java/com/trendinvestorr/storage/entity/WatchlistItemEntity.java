package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.time.Instant;

/** Belongs-to row joining a Watchlist to a Stock. */
@Entity
@Table(
    name = "watchlist_items",
    uniqueConstraints = @UniqueConstraint(name = "watchlist_items_watchlist_stock_key",
                                          columnNames = {"watchlist_id", "stock_id"})
)
@Getter
@Setter
@NoArgsConstructor
public class WatchlistItemEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "watchlist_id", nullable = false)
    private WatchlistEntity watchlist;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "stock_id", nullable = false)
    private StockEntity stock;

    @Column(name = "source", length = 30)
    private String source = "manual";

    @Column(name = "priority")
    private Integer priority = 0;

    @Column(name = "added_at", updatable = false, insertable = false)
    private Instant addedAt;

    @Column(name = "notes", columnDefinition = "TEXT")
    private String notes = "";
}
