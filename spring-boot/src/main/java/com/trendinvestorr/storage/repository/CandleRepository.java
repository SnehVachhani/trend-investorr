package com.trendinvestorr.storage.repository;

import com.trendinvestorr.core.constants.CandleInterval;
import com.trendinvestorr.storage.entity.*;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Optional;

/**
 * Routing facade over the six interval-specific candle repositories.
 * Delegates to the right repo based on {@link CandleInterval}.
 *
 * <p>Replaces Python {@code CandleStore} (which had per-interval query methods
 * guarded by a SQL-table-suffix lookup).
 */
@Repository
public class CandleRepository {

    private final Candle1mRepository r1m;
    private final Candle5mRepository r5m;
    private final Candle15mRepository r15m;
    private final Candle1hRepository r1h;
    private final Candle1dRepository r1d;
    private final Candle1wRepository r1w;

    public CandleRepository(Candle1mRepository r1m, Candle5mRepository r5m, Candle15mRepository r15m,
                            Candle1hRepository r1h, Candle1dRepository r1d, Candle1wRepository r1w) {
        this.r1m = r1m; this.r5m = r5m; this.r15m = r15m;
        this.r1h = r1h; this.r1d = r1d; this.r1w = r1w;
    }

    /** Save a single candle row across the correct interval table. */
    public AbstractCandleEntity save(CandleInterval interval, AbstractCandleEntity entity) {
        return switch (interval) {
            case ONE_MINUTE -> r1m.save((Candle1mEntity) entity);
            case FIVE_MINUTE -> r5m.save((Candle5mEntity) entity);
            case FIFTEEN_MINUTE -> r15m.save((Candle15mEntity) entity);
            case ONE_HOUR -> r1h.save((Candle1hEntity) entity);
            case ONE_DAY -> r1d.save((Candle1dEntity) entity);
            case ONE_WEEK -> r1w.save((Candle1wEntity) entity);
        };
    }

    /** Batch-save rows across the correct interval table. */
    @SuppressWarnings({"unchecked", "rawtypes"})
    public List<? extends AbstractCandleEntity> saveAll(CandleInterval interval,
                                                        List<? extends AbstractCandleEntity> entities) {
        JpaRepository repo = switch (interval) {
            case ONE_MINUTE -> r1m;
            case FIVE_MINUTE -> r5m;
            case FIFTEEN_MINUTE -> r15m;
            case ONE_HOUR -> r1h;
            case ONE_DAY -> r1d;
            case ONE_WEEK -> r1w;
        };
        return (List<? extends AbstractCandleEntity>) repo.saveAll((List) entities);
    }

    /** Lookup a candle by (stockId, timestamp) on the given interval. */
    public Optional<? extends AbstractCandleEntity> findByStockAndTimestamp(CandleInterval interval,
                                                                            Long stockId, Instant ts) {
        return switch (interval) {
            case ONE_MINUTE -> r1m.findByStockIdAndTimestamp(stockId, ts);
            case FIVE_MINUTE -> r5m.findByStockIdAndTimestamp(stockId, ts);
            case FIFTEEN_MINUTE -> r15m.findByStockIdAndTimestamp(stockId, ts);
            case ONE_HOUR -> r1h.findByStockIdAndTimestamp(stockId, ts);
            case ONE_DAY -> r1d.findByStockIdAndTimestamp(stockId, ts);
            case ONE_WEEK -> r1w.findByStockIdAndTimestamp(stockId, ts);
        };
    }

    /** Fetch a time-window slice for a stock on the given interval. */
    public List<? extends AbstractCandleEntity> findByStockBetween(CandleInterval interval, Long stockId,
                                                                  Instant from, Instant to) {
        return switch (interval) {
            case ONE_MINUTE -> r1m.findByStockIdAndTimestampBetweenOrderByTimestampAsc(stockId, from, to);
            case FIVE_MINUTE -> r5m.findByStockIdAndTimestampBetweenOrderByTimestampAsc(stockId, from, to);
            case FIFTEEN_MINUTE -> r15m.findByStockIdAndTimestampBetweenOrderByTimestampAsc(stockId, from, to);
            case ONE_HOUR -> r1h.findByStockIdAndTimestampBetweenOrderByTimestampAsc(stockId, from, to);
            case ONE_DAY -> r1d.findByStockIdAndTimestampBetweenOrderByTimestampAsc(stockId, from, to);
            case ONE_WEEK -> r1w.findByStockIdAndTimestampBetweenOrderByTimestampAsc(stockId, from, to);
        };
    }

    /** Latest-N rows on the given interval, returned in ascending timestamp order
     *  (oldest → newest of the slice, ready for indicator computation). */
    public List<? extends AbstractCandleEntity> findRecent(CandleInterval interval, Long stockId, int count) {
        List<? extends AbstractCandleEntity> desc = switch (interval) {
            case ONE_MINUTE -> r1m.findTop60ByStockIdOrderByTimestampDesc(stockId);
            case FIVE_MINUTE -> r5m.findTop200ByStockIdOrderByTimestampDesc(stockId);
            case FIFTEEN_MINUTE -> r15m.findTop100ByStockIdOrderByTimestampDesc(stockId);
            case ONE_HOUR -> r1h.findTop200ByStockIdOrderByTimestampDesc(stockId);
            case ONE_DAY -> r1d.findTop730ByStockIdOrderByTimestampDesc(stockId);
            case ONE_WEEK -> r1w.findTop104ByStockIdOrderByTimestampDesc(stockId);
        };
        List<? extends AbstractCandleEntity> trimmed =
                desc.size() > count ? desc.subList(0, count) : desc;
        // Convert DESC slice → ASC. Java 17 has no List.reversed(); copy + reverse in place.
        List<AbstractCandleEntity> asc = new ArrayList<>(trimmed);
        Collections.reverse(asc);
        return asc;
    }

    /** New empty entity for the requested interval (used by ingest writers). */
    public AbstractCandleEntity newEntity(CandleInterval interval) {
        return switch (interval) {
            case ONE_MINUTE -> new Candle1mEntity();
            case FIVE_MINUTE -> new Candle5mEntity();
            case FIFTEEN_MINUTE -> new Candle15mEntity();
            case ONE_HOUR -> new Candle1hEntity();
            case ONE_DAY -> new Candle1dEntity();
            case ONE_WEEK -> new Candle1wEntity();
        };
    }
}
