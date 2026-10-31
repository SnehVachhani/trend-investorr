package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/** A named collection of stocks (parent side of watchlist ↔ watchlist_items). */
@Entity
@Table(name = "watchlists")
@Getter
@Setter
@NoArgsConstructor
public class WatchlistEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "name", nullable = false, unique = true, length = 100)
    private String name;

    @Column(name = "description", columnDefinition = "TEXT")
    private String description = "";

    @Column(name = "list_type", length = 30)
    private String listType = "custom";

    @Column(name = "is_active")
    private Boolean isActive = true;

    @Column(name = "max_stocks")
    private Integer maxStocks = 500;

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at")
    private Instant updatedAt;
}
