package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.PositionEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/** Position repository — replaces the position-tracking side of {@code OrderStore} in Python. */
@Repository
public interface PositionRepository extends JpaRepository<PositionEntity, Long> {

    List<PositionEntity> findByIsPaper(Boolean isPaper);

    Optional<PositionEntity> findByStockIdAndIsPaperAndProductType(Long stockId, Boolean isPaper, String productType);

    boolean existsByStockIdAndIsPaperAndProductType(Long stockId, Boolean isPaper, String productType);

    List<PositionEntity> findByUnrealizedPnlLessThan(Double threshold);

    long countByIsPaper(Boolean isPaper);
}
