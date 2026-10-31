package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.WatchlistItemEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/** Watchlist-items repository — replaces the join-side methods of {@code WatchlistStore}. */
@Repository
public interface WatchlistItemRepository extends JpaRepository<WatchlistItemEntity, Long> {

    List<WatchlistItemEntity> findByWatchlistIdOrderByPriorityDescAddedAtAsc(Long watchlistId);

    Optional<WatchlistItemEntity> findByWatchlistIdAndStockId(Long watchlistId, Long stockId);

    boolean existsByWatchlistIdAndStockId(Long watchlistId, Long stockId);

    long countByWatchlistId(Long watchlistId);

    void deleteByWatchlistIdAndStockId(Long watchlistId, Long stockId);

    void deleteByWatchlistId(Long watchlistId);

    @Modifying
    @Query("UPDATE WatchlistItemEntity w SET w.priority = :priority WHERE w.id = :id")
    int updatePriority(@Param("id") Long id, @Param("priority") int priority);
}
