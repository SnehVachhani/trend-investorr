package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.CandleCoverageEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/** Coverage tracker — replaces Python {@code CandleCoverageStore}. */
@Repository
public interface CandleCoverageRepository extends JpaRepository<CandleCoverageEntity, Long> {

    Optional<CandleCoverageEntity> findByStockIdAndInterval(Long stockId, String interval);

    List<CandleCoverageEntity> findByInterval(String interval);

    @Query("""
           SELECT c FROM CandleCoverageEntity c
           WHERE c.interval = :interval AND c.stock.id IN :stockIds
           """)
    List<CandleCoverageEntity> findByStockIdsAndInterval(@Param("stockIds") List<Long> stockIds,
                                                        @Param("interval") String interval);

    @Query("""
           SELECT COUNT(c) FROM CandleCoverageEntity c
           WHERE c.interval = :interval AND c.candleCount > 0
           """)
    long countCovered(@Param("interval") String interval);
}
