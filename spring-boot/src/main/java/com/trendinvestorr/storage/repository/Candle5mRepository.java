package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.Candle5mEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

@Repository
public interface Candle5mRepository extends JpaRepository<Candle5mEntity, Long> {
    Optional<Candle5mEntity> findByStockIdAndTimestamp(Long stockId, Instant ts);
    List<Candle5mEntity> findByStockIdAndTimestampAfterOrderByTimestampAsc(Long stockId, Instant from);
    List<Candle5mEntity> findByStockIdAndTimestampBetweenOrderByTimestampAsc(Long stockId, Instant from, Instant to);
    List<Candle5mEntity> findTop200ByStockIdOrderByTimestampDesc(Long stockId);
}
