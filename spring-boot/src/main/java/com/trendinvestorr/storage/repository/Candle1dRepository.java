package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.Candle1dEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

@Repository
public interface Candle1dRepository extends JpaRepository<Candle1dEntity, Long> {
    Optional<Candle1dEntity> findByStockIdAndTimestamp(Long stockId, Instant ts);
    List<Candle1dEntity> findByStockIdAndTimestampAfterOrderByTimestampAsc(Long stockId, Instant from);
    List<Candle1dEntity> findByStockIdAndTimestampBetweenOrderByTimestampAsc(Long stockId, Instant from, Instant to);
    List<Candle1dEntity> findTop730ByStockIdOrderByTimestampDesc(Long stockId); // ~3 years

    /** Average volume across the most recent {@code n} daily candles. */
    @Query("SELECT COALESCE(AVG(c.volume), 0) FROM Candle1dEntity c " +
           "WHERE c.stock.id = :stockId ORDER BY c.timestamp DESC LIMIT :n")
    Long averageVolumeTopN(@Param("stockId") Long stockId, @Param("n") int n);
}
