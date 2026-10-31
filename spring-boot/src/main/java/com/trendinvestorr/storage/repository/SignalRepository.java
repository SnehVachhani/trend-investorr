package com.trendinvestorr.storage.repository;

import com.trendinvestorr.core.constants.SignalStatus;
import com.trendinvestorr.core.constants.SignalStrength;
import com.trendinvestorr.core.constants.SignalType;
import com.trendinvestorr.storage.entity.SignalEntity;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;

/** Signal repository — replaces Python {@code SignalStore + ScannerSignalStore}. */
@Repository
public interface SignalRepository extends JpaRepository<SignalEntity, Long> {

    List<SignalEntity> findByStatus(SignalStatus status);

    List<SignalEntity> findBySignalType(SignalType signalType);

    Page<SignalEntity> findByStatusOrderByCreatedAtDesc(SignalStatus status, Pageable pageable);

    Page<SignalEntity> findAllByOrderByCreatedAtDesc(Pageable pageable);

    @Query("SELECT s FROM SignalEntity s WHERE s.createdAt >= :from ORDER BY s.createdAt DESC")
    List<SignalEntity> findSince(@Param("from") Instant from);

    @Query("""
           SELECT s FROM SignalEntity s
           WHERE (:source IS NULL OR s.analyzerName = :source)
           AND (:status IS NULL OR s.status = :status)
           AND (:strength IS NULL OR s.strength = :strength)
           AND (:from IS NULL OR s.createdAt >= :from)
           ORDER BY s.createdAt DESC
           """)
    Page<SignalEntity> searchSignals(@Param("source") String source,
                                     @Param("status") SignalStatus status,
                                     @Param("strength") SignalStrength strength,
                                     @Param("from") Instant from,
                                     Pageable pageable);

    long countByStatus(SignalStatus status);

    long countBySignalType(SignalType signalType);
}
