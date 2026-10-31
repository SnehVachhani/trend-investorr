package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.DailyPnlEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.LocalDate;
import java.util.List;
import java.util.Optional;

/** Daily P&L repository — replaces Python {@code DailyPnLStore}. */
@Repository
public interface DailyPnlRepository extends JpaRepository<DailyPnlEntity, Long> {

    Optional<DailyPnlEntity> findByDateAndIsPaper(LocalDate date, Boolean isPaper);

    List<DailyPnlEntity> findByIsPaperOrderByDateDesc(Boolean isPaper);

    List<DailyPnlEntity> findByDateBetweenOrderByDateAsc(LocalDate from, LocalDate to);

    Optional<DailyPnlEntity> findTopByDateAndIsPaperOrderByDateDesc(LocalDate date, Boolean isPaper);
}
