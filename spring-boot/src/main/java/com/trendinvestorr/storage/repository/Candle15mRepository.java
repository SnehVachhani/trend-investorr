package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.Candle15mEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

@Repository
public interface Candle15mRepository extends JpaRepository<Candle15mEntity, Long> {
    Optional<Candle15mEntity> findByStockIdAndTimestamp(Long stockId, Instant ts);
    List<Candle15mEntity> findByStockIdAndTimestampAfterOrderByTimestampAsc(Long stockId, Instant from);
    List<Candle15mEntity> findByStockIdAndTimestampBetweenOrderByTimestampAsc(Long stockId, Instant from, Instant to);
    List<Candle15mEntity> findTop100ByStockIdOrderByTimestampDesc(Long stockId);
}
