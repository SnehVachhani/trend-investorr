package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.LiveTickEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;

/**
 * Live-tick repository — replaces Python {@code TickStore}.
 *
 * <p>Raw WebSocket data is disposable; rows older than {@code data.tick-retention-days}
 * (default 3 days) are purged every pre-market via {@link #purgeBefore(Instant)}.
 */
@Repository
public interface LiveTickRepository extends JpaRepository<LiveTickEntity, Long> {

    List<LiveTickEntity> findByTokenOrderByTimestampDesc(String token);

    List<LiveTickEntity> findByTokenAndTimestampAfterOrderByTimestampAsc(String token, Instant from);

    @Query("SELECT t FROM LiveTickEntity t WHERE t.timestamp >= :from AND t.timestamp <= :to ORDER BY t.timestamp ASC")
    List<LiveTickEntity> findBetween(@Param("from") Instant from, @Param("to") Instant to);

    @Modifying
    @Query("DELETE FROM LiveTickEntity t WHERE t.timestamp < :before")
    int purgeBefore(@Param("before") Instant before);
}
