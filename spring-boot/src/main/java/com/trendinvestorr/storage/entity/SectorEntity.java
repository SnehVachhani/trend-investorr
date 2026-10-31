package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/**
 * NSE sector master. Maps to the {@code sectors} table.
 * Port of the Python {@code stocks} FK to {@code sectors} and the {@code SectorStore} lookups.
 */
@Entity
@Table(name = "sectors")
@Getter
@Setter
@NoArgsConstructor
public class SectorEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "name", nullable = false, unique = true, length = 50)
    private String name;

    @Column(name = "display_name", nullable = false, length = 100)
    private String displayName;

    @Column(name = "nifty_index", length = 50)
    private String niftyIndex = "";

    @Column(name = "is_active")
    private Boolean isActive = true;

    @Column(name = "created_at", updatable = false, insertable = false)
    private Instant createdAt;
}
