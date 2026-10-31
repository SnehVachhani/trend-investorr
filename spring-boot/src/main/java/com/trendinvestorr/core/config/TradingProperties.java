package com.trendinvestorr.core.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * Risk-management constants and circuit-breaker thresholds.
 * Port of the Python {@code RiskConfig} and {@code CircuitBreakerConfig} dataclasses
 * passed into RiskManager / CircuitBreaker in {@code main.py} Step 8.
 */
@Component
@ConfigurationProperties(prefix = "trading")
public record TradingProperties(
        double maxCapitalPerTradePct,
        double maxDailyLossPct,
        int maxOpenPositions,
        double defaultStopLossPct,
        double defaultTargetPct,
        double slippagePct,
        CircuitBreakerProperties circuitBreaker
) {
    public TradingProperties {
        if (maxCapitalPerTradePct == 0.0) maxCapitalPerTradePct = 5.0;
        if (maxDailyLossPct == 0.0) maxDailyLossPct = 2.0;
        if (maxOpenPositions == 0) maxOpenPositions = 20;
        if (defaultStopLossPct == 0.0) defaultStopLossPct = 2.0;
        if (defaultTargetPct == 0.0) defaultTargetPct = 5.0;
        if (slippagePct == 0.0) slippagePct = 0.1;
        if (circuitBreaker == null) {
            circuitBreaker = new CircuitBreakerProperties(3.0, 5);
        }
    }

    public record CircuitBreakerProperties(
            double dailyLossPct,
            int consecutiveLosses
    ) {}
}
