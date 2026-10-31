package com.trendinvestorr.storage.repository;

import com.trendinvestorr.storage.entity.AppConfigEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/** Runtime-config repository — replaces Python {@code AppConfigStore}. */
@Repository
public interface AppConfigRepository extends JpaRepository<AppConfigEntity, Long> {

    Optional<AppConfigEntity> findByKey(String key);

    List<AppConfigEntity> findByCategory(String category);

    boolean existsByKey(String key);

    @Query("SELECT a FROM AppConfigEntity a ORDER BY a.category, a.key")
    List<AppConfigEntity> findAllOrdered();

    /** Convenience for in-place value updates without re-fetching. */
    @org.springframework.data.jpa.repository.Modifying
    @Query("UPDATE AppConfigEntity a SET a.value = :value WHERE a.key = :key")
    int updateValue(@Param("key") String key, @Param("value") String value);
}
