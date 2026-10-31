package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.TechnicalIndicatorEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.LocalDate;
import java.util.List;
import java.util.Optional;

/**
 * Technical-indicator repository — replaces Python {@code TechnicalIndicatorStore}.
 * Loaded into the in-memory {@code _indicator_cache} at the 9:00 AM warm-up for
 * zero-latency hot-path access from scanners / analyzers.
 */
@Repository
public interface TechnicalIndicatorRepository extends JpaRepository<TechnicalIndicatorEntity, Long> {

    Optional<TechnicalIndicatorEntity> findByStockIdAndDate(Long stockId, LocalDate date);

    @Query("""
           SELECT t FROM TechnicalIndicatorEntity t
           WHERE t.stock.id = :stockId AND t.date <= :asOf
           ORDER BY t.date DESC
           LIMIT 1
           """)
    Optional<TechnicalIndicatorEntity> findMostRecentBefore(@Param("stockId") Long stockId,
                                                            @Param("asOf") LocalDate asOf);

    @Query("""
           SELECT t FROM TechnicalIndicatorEntity t
           WHERE t.stock.id = :stockId AND t.date >= :from
           ORDER BY t.date ASC
           """)
    List<TechnicalIndicatorEntity> findRangeAsc(@Param("stockId") Long stockId,
                                                 @Param("from") LocalDate from);

    @Query("""
           SELECT t FROM TechnicalIndicatorEntity t
           WHERE t.date = (SELECT MAX(t2.date) FROM TechnicalIndicatorEntity t2
                           WHERE t2.stock.id = :stockId AND t2.date <= :asOf)
           """)
    Optional<TechnicalIndicatorEntity> findLatestForStock(@Param("stockId") Long stockId,
                                                          @Param("asOf") LocalDate asOf);

    @Query("""
           SELECT t FROM TechnicalIndicatorEntity t
           WHERE t.date = (SELECT MAX(t2.date) FROM TechnicalIndicatorEntity t2)
           """)
    List<TechnicalIndicatorEntity> findLatestForAllStocks();
}
