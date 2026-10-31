package com.trendinvestorr.storage.repository;

import com.trendinvestorr.core.constants.OrderStatus;
import com.trendinvestorr.core.constants.TransactionType;
import com.trendinvestorr.storage.entity.OrderEntity;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.List;

/** Order repository — replaces Python {@code OrderStore}. */
@Repository
public interface OrderRepository extends JpaRepository<OrderEntity, Long> {

    List<OrderEntity> findByStatus(OrderStatus status);

    Page<OrderEntity> findAllByOrderByCreatedAtDesc(Pageable pageable);

    List<OrderEntity> findByStockIdOrderByCreatedAtDesc(Long stockId);

    List<OrderEntity> findByIsPaperTradeAndCreatedAtAfter(Boolean isPaper, Instant from);

    long countByStatus(OrderStatus status);

    long countByTransactionType(TransactionType transactionType);

    List<OrderEntity> findByStatusIn(List<OrderStatus> statuses);
}
