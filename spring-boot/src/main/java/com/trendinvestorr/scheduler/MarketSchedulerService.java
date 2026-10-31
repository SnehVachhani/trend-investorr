package com.trendinvestorr.scheduler;

import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.storage.service.AuditTrailService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Market-day orchestrator — Spring Boot port of Python {@code market_scheduler.py}.
 *
 * <p>Replaces APScheduler {@code BackgroundScheduler} with Spring's {@link Scheduled}.
 * Eight cron-triggered phases run on IST weekday boundaries (see {@link MarketLifecyclePort}).
 * The class itself knows nothing about data ingestion, strategy, or notifications — it only
 * fans out to registered {@link MarketLifecyclePort} implementations, mirroring the Python
 * {@code register_*} callbacks. Open–Closed: a new subsystem joins the day by contributing a
 * {@code @Component} that implements the lifecycle port — no edit to this class required.
 *
 * <p>Every phase is wrapped in try/catch and audited via {@link AuditTrailService} so a single
 * failing phase can neither cascade into subsequent phases nor escape silently (Python behavior).
 * Spring cron expressions use 6 fields (sec min hour day month weekday); the IST zone is applied
 * globally via {@code spring.jackson.time-zone} / JVM default set in {@code TrendInvestorrApplication}.
 */
@Service
public class MarketSchedulerService {

    private static final Logger log = LoggerFactory.getLogger(MarketSchedulerService.class);
    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private final AuditTrailService audit;
    private final List<MarketLifecyclePort> lifecycleHooks;

    public MarketSchedulerService(AuditTrailService audit, List<MarketLifecyclePort> lifecycleHooks) {
        this.audit = audit;
        this.lifecycleHooks = lifecycleHooks == null ? new CopyOnWriteArrayList<>() : new CopyOnWriteArrayList<>(lifecycleHooks);
        log.info("MarketSchedulerService wired with {} lifecycle hook(s)", this.lifecycleHooks.size());
    }

    /** 08:30 IST, weekdays. */
    @Scheduled(cron = "0 30 8 ? * MON-FRI", zone = "Asia/Kolkata")
    public void preMarketBoot() {
        runPhase(AuditEventType.SCHEDULER_TASK, "pre_market_boot", MarketLifecyclePort::preMarketBoot);
    }

    /** 09:00 IST, weekdays. */
    @Scheduled(cron = "0 0 9 ? * MON-FRI", zone = "Asia/Kolkata")
    public void scannerWarmup() {
        runPhase(AuditEventType.SCHEDULER_TASK, "scanner_warmup", MarketLifecyclePort::scannerWarmup);
    }

    /** 09:15 IST, weekdays — market opens. */
    @Scheduled(cron = "0 15 9 ? * MON-FRI", zone = "Asia/Kolkata")
    public void marketOpen() {
        runPhase(AuditEventType.SCHEDULER_TASK, "market_open", MarketLifecyclePort::marketOpen);
    }

    /** 15:15 IST, weekdays — square off intraday 15 min before close. */
    @Scheduled(cron = "0 15 15 ? * MON-FRI", zone = "Asia/Kolkata")
    public void intradaySquareOff() {
        runPhase(AuditEventType.SCHEDULER_TASK, "intraday_squareoff", MarketLifecyclePort::intradaySquareOff);
    }

    /** 15:30 IST, weekdays — market closes. */
    @Scheduled(cron = "0 30 15 ? * MON-FRI", zone = "Asia/Kolkata")
    public void marketClose() {
        runPhase(AuditEventType.SCHEDULER_TASK, "market_close", MarketLifecyclePort::marketClose);
    }

    /** 16:00 IST, weekdays — post-market bhavcopy + aggregation. */
    @Scheduled(cron = "0 0 16 ? * MON-FRI", zone = "Asia/Kolkata")
    public void postMarketAnalysis() {
        runPhase(AuditEventType.SCHEDULER_TASK, "post_market_analysis", MarketLifecyclePort::postMarketAnalysis);
    }

    /** 17:00 IST, weekdays — daily P&L summary. */
    @Scheduled(cron = "0 0 17 ? * MON-FRI", zone = "Asia/Kolkata")
    public void dailySummary() {
        runPhase(AuditEventType.SCHEDULER_TASK, "daily_summary", MarketLifecyclePort::dailySummary);
    }

    /** 00:00 IST daily — session re-login, counter/circuit-breaker reset. */
    @Scheduled(cron = "0 0 0 * * ?", zone = "Asia/Kolkata")
    public void midnightReset() {
        runPhase(AuditEventType.SCHEDULER_TASK, "midnight_reset", MarketLifecyclePort::midnightReset);
    }

    /** Manual trigger — mirrors Python {@code run_now(job_name)}. */
    public void runNow(String phaseName) {
        switch (phaseName) {
            case "pre_market_boot" -> preMarketBoot();
            case "scanner_warmup" -> scannerWarmup();
            case "market_open" -> marketOpen();
            case "intraday_squareoff" -> intradaySquareOff();
            case "market_close" -> marketClose();
            case "post_market_analysis" -> postMarketAnalysis();
            case "daily_summary" -> dailySummary();
            case "midnight_reset" -> midnightReset();
            default -> log.warn("Unknown scheduler phase '{}' — ignored", phaseName);
        }
    }

    /** Stats exposed to /api/system/status — mirrors Python {@code MarketScheduler.stats}. */
    public Map<String, Object> stats() {
        return Map.of(
                "phase_count", 8,
                "registered_hooks", lifecycleHooks.size(),
                "phases", List.of(
                        "pre_market_boot", "scanner_warmup", "market_open",
                        "intraday_squareoff", "market_close", "post_market_analysis",
                        "daily_summary", "midnight_reset")
        );
    }

    private void runPhase(AuditEventType eventType, String phaseName,
                          java.util.function.Consumer<MarketLifecyclePort> action) {
        String stamp = LocalDateTime.now().format(FMT);
        log.info("Phase '{}' started at {}", phaseName, stamp);
        int ok = 0, fail = 0;
        for (MarketLifecyclePort hook : lifecycleHooks) {
            try {
                action.accept(hook);
                ok++;
            } catch (Exception e) {
                fail++;
                log.error("Phase '{}' hook {} failed: {}", phaseName,
                        hook.getClass().getSimpleName(), e.getMessage(), e);
                audit.log(eventType, Map.of(
                        "phase", phaseName,
                        "hook", hook.getClass().getSimpleName(),
                        "status", "FAILED",
                        "error", e.getMessage() == null ? "" : e.getMessage()));
            }
        }
        audit.log(eventType, Map.of("phase", phaseName, "ok", ok, "failed", fail));
        log.info("Phase '{}' complete (ok={}, failed={})", phaseName, ok, fail);
    }

    public List<MarketLifecyclePort> hooks() {
        return List.copyOf(lifecycleHooks);
    }
}
