package com.trendinvestorr.storage.repository;

import com.trendinvestorr.core.constants.Exchange;
import com.trendinvestorr.storage.entity.SectorEntity;
import com.trendinvestorr.storage.entity.StockEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/**
 * Stock repository — replaces Python {@code StockStore}.
 */
@Repository
public interface StockRepository extends JpaRepository<StockEntity, Long> {

    Optional<StockEntity> findBySymbolAndExchange(String symbol, Exchange exchange);

    Optional<StockEntity> findBySymbol(String symbol);

    Optional<StockEntity> findByToken(String token);

    List<StockEntity> findByIsActiveTrue();

    List<StockEntity> findByIsActiveTrueAndExchange(Exchange exchange);

    List<StockEntity> findByExchange(Exchange exchange);

    List<StockEntity> findBySymbolContainingIgnoreCase(String fragment);

    @Query("SELECT s FROM StockEntity s WHERE LOWER(s.symbol) LIKE LOWER(CONCAT('%', :frag, '%')) "
         + "ORDER BY s.symbol")
    List<StockEntity> searchBySymbol(@Param("frag") String fragment);

    @Query("SELECT s FROM StockEntity s LEFT JOIN FETCH s.sector WHERE s.isActive = true")
    List<StockEntity> findAllActiveWithSector();

    @Query("SELECT s FROM StockEntity s LEFT JOIN FETCH s.sector WHERE s.isActive = true AND s.exchange = :ex")
    List<StockEntity> findActiveByExchangeWithSector(@Param("ex") Exchange ex);

    @Query("SELECT s.token FROM StockEntity s WHERE s.isActive = true")
    List<String> findAllActiveTokens();

    @Query(value = """
            SELECT s.* FROM stocks s
            JOIN watchlist_items wi ON wi.stock_id = s.id
            WHERE wi.watchlist_id = :watchlistId
            ORDER BY wi.priority DESC, wi.added_at ASC
            """, nativeQuery = true)
    List<StockEntity> findByWatchlistId(@Param("watchlistId") Long watchlistId);

    void deleteByIdIn(List<Long> ids);
}
