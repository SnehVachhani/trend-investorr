package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.Candle1hEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

@Repository
public interface Candle1hRepository extends JpaRepository<Candle1hEntity, Long> {
    Optional<Candle1hEntity> findByStockIdAndTimestamp(Long stockId, Instant ts);
    List<Candle1hEntity> findByStockIdAndTimestampAfterOrderByTimestampAsc(Long stockId, Instant from);
    List<Candle1hEntity> findByStockIdAndTimestampBetweenOrderByTimestampAsc(Long stockId, Instant from, Instant to);
    List<Candle1hEntity> findTop200ByStockIdOrderByTimestampDesc(Long stockId);
}
