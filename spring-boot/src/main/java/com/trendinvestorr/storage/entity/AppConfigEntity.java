package com.trendinvestorr.storage.entity;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.Instant;

/** Runtime-tunable parameter row. Replaces the {@code app_config} DB table reads in Python. */
@Entity
@Table(name = "app_config")
@Getter
@Setter
@NoArgsConstructor
public class AppConfigEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "key", nullable = false, unique = true, length = 100)
    private String key;

    @Column(name = "value", nullable = false, columnDefinition = "TEXT")
    private String value;

    @Column(name = "data_type", nullable = false, length = 20)
    private String dataType = "string";

    @Column(name = "category", length = 50)
    private String category = "general";

    @Column(name = "description", columnDefinition = "TEXT")
    private String description = "";

    @UpdateTimestamp
    @Column(name = "updated_at")
    private Instant updatedAt;

    @Column(name = "updated_by", length = 50)
    private String updatedBy = "system";
}
