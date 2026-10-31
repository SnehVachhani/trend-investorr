package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.Candle1mEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

@Repository
public interface Candle1mRepository extends JpaRepository<Candle1mEntity, Long> {
    Optional<Candle1mEntity> findByStockIdAndTimestamp(Long stockId, Instant ts);
    List<Candle1mEntity> findByStockIdAndTimestampAfterOrderByTimestampAsc(Long stockId, Instant from);
    List<Candle1mEntity> findByStockIdAndTimestampBetweenOrderByTimestampAsc(Long stockId, Instant from, Instant to);
    List<Candle1mEntity> findTop60ByStockIdOrderByTimestampDesc(Long stockId);
}
