package com.trendinvestorr;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * Trend-Investorr — Spring Boot port of the Python volume-first trading bot.
 *
 * <p>Layer mapping from the Python project:
 * <ul>
 *   <li>core/config, core/security, core/constants  → com.trendinvestorr.core
 *   <li>storage (Database + models.py)              → com.trendinvestorr.storage (JPA + repos)
 *   <li>data (auth, fetchers, streams)               → com.trendinvestorr.data
 *   <li>engine (scanners + indicator engine)        → com.trendinvestorr.engine
 *   <li>strategies (AlgorithmManager + algos)        → com.trendinvestorr.strategy
 *   <li>analyzers (static / gemini / composite)      → com.trendinvestorr.analyzer
 *   <li>trading (risk, circuit breaker, orders)      → com.trendinvestorr.trading
 *   <li>scheduler (APScheduler jobs)                → com.trendinvestorr.scheduler (@Scheduled / Quartz)
 *   <li>notifications (Telegram)                    → com.trendinvestorr.notification
 *   <li>api/server.py (FastAPI)                     → com.trendinvestorr.api (REST controllers)
 * </ul>
 *
 * <p>The Python {@code main.py} orchestrator is replaced by Spring's DI container:
 * each subsystem is a {@code @Component}/{@code @Service} wired through constructor
 * injection, and the daily lifecycle is driven by {@link EnableScheduling}.
 */
@SpringBootApplication
@ConfigurationPropertiesScan
@EnableScheduling
public class TrendInvestorrApplication {

    public static void main(String[] args) {
        SpringApplication.run(TrendInvestorrApplication.class, args);
    }
}
