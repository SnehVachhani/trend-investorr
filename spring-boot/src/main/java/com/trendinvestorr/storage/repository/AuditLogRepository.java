package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.AuditLogEntity;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;

/** Audit-log repository — replaces Python {@code AuditLogStore}. Append-only by convention. */
@Repository
public interface AuditLogRepository extends JpaRepository<AuditLogEntity, Long> {

    List<AuditLogEntity> findByEventTypeOrderByTimestampDesc(String eventType);

    Page<AuditLogEntity> findAllByOrderByTimestampDesc(Pageable pageable);

    List<AuditLogEntity> findByEventTypeAndTimestampAfterOrderByTimestampDesc(String eventType, Instant from);

    long countByEventType(String eventType);
}
