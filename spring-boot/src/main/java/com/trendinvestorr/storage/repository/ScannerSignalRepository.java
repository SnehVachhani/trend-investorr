package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.ScannerSignalEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.LocalDate;
import java.util.List;

/** Scanner-signal repository — replaces Python {@code ScannerSignalStore}. */
@Repository
public interface ScannerSignalRepository extends JpaRepository<ScannerSignalEntity, Long> {

    @Query("""
           SELECT s FROM ScannerSignalEntity s
           WHERE s.targetDate = :target AND s.status = 'ACTIVE'
           ORDER BY s.confidence DESC
           """)
    List<ScannerSignalEntity> findActiveForTargetDate(@Param("target") LocalDate target);

    @Query("""
           SELECT s FROM ScannerSignalEntity s
           WHERE s.scanDate = :date AND s.status = 'ACTIVE'
           ORDER BY s.confidence DESC
           """)
    List<ScannerSignalEntity> findActiveForScanDate(@Param("date") LocalDate date);

    @Modifying
    @Query("UPDATE ScannerSignalEntity s SET s.status = 'CONSUMED' WHERE s.id IN :ids")
    int markConsumed(@Param("ids") List<Long> ids);

    @Modifying
    @Query("UPDATE ScannerSignalEntity s SET s.status = 'EXPIRED' WHERE s.targetDate < :before")
    int expireOlderThan(@Param("before") LocalDate before);
}
