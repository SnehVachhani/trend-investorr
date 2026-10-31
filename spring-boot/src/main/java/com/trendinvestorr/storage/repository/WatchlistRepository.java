package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.WatchlistEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/** Watchlist metadata repository — replaces Python {@code WatchlistStore}. */
@Repository
public interface WatchlistRepository extends JpaRepository<WatchlistEntity, Long> {

    Optional<WatchlistEntity> findByName(String name);

    Optional<WatchlistEntity> findByNameIgnoreCase(String name);

    List<WatchlistEntity> findByIsActiveTrue();

    boolean existsByName(String name);
}
