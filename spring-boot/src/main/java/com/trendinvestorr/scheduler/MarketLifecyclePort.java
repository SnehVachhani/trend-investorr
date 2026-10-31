package com.trendinvestorr.scheduler;

/**
 * Market lifecycle port — Dependency-Inversion seam for {@link MarketSchedulerService}.
 *
 * <p>The Spring {@code @Scheduled} layer depends only on this abstraction; each subsystem
 * (data ingestion, strategy engine, notifier, …) supplies a concrete {@code @Component}
 * that implements the phase it owns. This keeps the scheduler open for extension
 * (new phases plug in without touching it) and closed for modification, and lets unit
 * tests drive each phase independently.
 *
 * <p>Implementations may throw; {@link MarketSchedulerService} catches and audits every
 * failure so one failing phase never cascades into silence for the next.
 */
public interface MarketLifecyclePort {

    /** 08:30 IST — instrument reload, SmartAPI session login, config refresh. */
    void preMarketBoot();

    /** 09:00 IST — load historical volumes, warm up indicators, subscribe WS feeds. */
    void scannerWarmup();

    /** 09:15 IST — start live scanning / strategy evaluation. */
    void marketOpen();

    /** 15:15 IST — square off intraday positions 15 min before close. */
    void intradaySquareOff();

    /** 15:30 IST — stop live scanning, flush tick buffers. */
    void marketClose();

    /** 16:00 IST — bhavcopy download, nightly aggregation, indicator recompute. */
    void postMarketAnalysis();

    /** 17:00 IST — daily P&L summary notification. */
    void dailySummary();

    /** 00:00 IST — session re-login, daily counters / circuit-breaker reset. */
    void midnightReset();
}
