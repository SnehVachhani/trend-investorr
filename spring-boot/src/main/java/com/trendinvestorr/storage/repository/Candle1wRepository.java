package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.Candle1wEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

@Repository
public interface Candle1wRepository extends JpaRepository<Candle1wEntity, Long> {
    Optional<Candle1wEntity> findByStockIdAndTimestamp(Long stockId, Instant ts);
    List<Candle1wEntity> findByStockIdAndTimestampAfterOrderByTimestampAsc(Long stockId, Instant from);
    List<Candle1wEntity> findByStockIdAndTimestampBetweenOrderByTimestampAsc(Long stockId, Instant from, Instant to);
    List<Candle1wEntity> findTop104ByStockIdOrderByTimestampDesc(Long stockId); // ~2 years
}
