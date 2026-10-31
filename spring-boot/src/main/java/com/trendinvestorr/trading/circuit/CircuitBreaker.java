package com.trendinvestorr.trading.circuit;

import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.core.constants.CircuitBreakerState;
import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.config.TradingProperties;
import com.trendinvestorr.core.exceptions.CircuitBreakerOpenError;
import com.trendinvestorr.storage.service.AuditTrailService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * System-level circuit breaker — auto-halts trading when loss, drawdown or
 * error-rate thresholds are exceeded. State machine:
 * {@code CLOSED → OPEN → HALF_OPEN → CLOSED}.
 *
 * <p><b>SOLID:</b> SRP — only trading-halt state; trigger evaluation is one
 * private method per trigger (OCP-friendly). Audit side-effects are delegated
 * to {@link AuditTrailService}; dependents talk to this breaker only through
 * {@link #check()}, {@link #positionSizeMultiplier()} and result recording.
 *
 * <p>Port of {@code trading/circuit_breaker.py}.
 */
@Service
public class CircuitBreaker {

    private static final Logger log = LoggerFactory.getLogger(CircuitBreaker.class);

    // Default total capital — runtime-tunable via TradingProperties in future.
    private static final double DEFAULT_CAPITAL = 1_00_00_000.0;

    private final AuditTrailService audit;
    private final TradingProperties props;
    private final double totalCapital;

    private volatile CircuitBreakerState state = CircuitBreakerState.CLOSED;
    private volatile Instant trippedAt = null;
    private volatile String tripReason = "";

    private volatile double dailyPnl = 0.0;
    private volatile double peakCapital = DEFAULT_CAPITAL;
    private volatile int consecutiveLosses = 0;
    private volatile int totalOrders = 0;
    private volatile int failedOrders = 0;
    private volatile int halfOpenTrades = 0;
    private volatile int tripCount = 0;

    public CircuitBreaker(AuditTrailService audit, TradingProperties props) {
        this(audit, props, DEFAULT_CAPITAL);
    }

    public CircuitBreaker(AuditTrailService audit, TradingProperties props, double totalCapital) {
        this.audit = audit;
        this.props = props;
        this.totalCapital = totalCapital;
        this.peakCapital = totalCapital;
    }

    // ── Public state API ──────────────────────────────────────

    public CircuitBreakerState state() { return state; }
    public boolean isOpen()     { return state == CircuitBreakerState.OPEN; }
    public boolean isClosed()   { return state == CircuitBreakerState.CLOSED; }
    public boolean isHalfOpen() { return state == CircuitBreakerState.HALF_OPEN; }

    /** Throw if OPEN — signals "no trading allowed right now". */
    public void check() {
        if (state == CircuitBreakerState.OPEN) {
            Map<String, Object> details = new LinkedHashMap<>();
            details.put("state", state.name());
            details.put("reason", tripReason);
            details.put("tripped_at", String.valueOf(trippedAt));
            details.put("daily_pnl", dailyPnl);
            details.put("consecutive_losses", consecutiveLosses);
            throw new CircuitBreakerOpenError(
                    "Circuit breaker OPEN — trading halted. Reason: " + tripReason
                            + ". Tripped at: " + trippedAt, 0);
        }
    }

    /** Position-size multiplier for the current state (1.0 / 0.5 / 0.0). */
    public double positionSizeMultiplier() {
        return switch (state) {
            case CLOSED    -> 1.0;
            case HALF_OPEN -> 0.5;
            case OPEN      -> 0.0;
        };
    }

    // ── Event recording ──────────────────────────────────────

    /** Record a completed trade's P&L and run all trigger checks. */
    public void recordTradeResult(double pnl) {
        dailyPnl += pnl;
        totalOrders++;
        if (pnl < 0) consecutiveLosses++;
        else consecutiveLosses = 0;

        double current = totalCapital + dailyPnl;
        if (current > peakCapital) peakCapital = current;

        checkDailyLossTrigger();
        checkConsecutiveLossTrigger();
        checkDrawdownTrigger();

        if (state == CircuitBreakerState.HALF_OPEN) {
            halfOpenTrades++;
            if (halfOpenTrades >= 2) {
                if (pnl >= 0) recover();
                else trip("Half-open recovery failed — re-halting");
            }
        }
    }

    /** Record a failed order (API error / rejection). */
    public void recordOrderFailure() {
        failedOrders++;
        totalOrders++;
        checkErrorRateTrigger();
    }

    // ── State transitions ─────────────────────────────────────

    public void trip(String reason) {
        if (state == CircuitBreakerState.OPEN) return;
        state = CircuitBreakerState.OPEN;
        trippedAt = Instant.now().atZone(MarketConstants.IST).toInstant();
        tripReason = reason;
        tripCount++;
        log.warn("🔴 CIRCUIT BREAKER TRIPPED: {}", reason);
        if (audit != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("new_state", state.name());
            d.put("reason", reason);
            d.put("daily_pnl", dailyPnl);
            d.put("consecutive_losses", consecutiveLosses);
            d.put("trip_count", tripCount);
            audit.log(AuditEventType.CIRCUIT_BREAKER_CHANGE, d);
        }
    }

    public void halfOpen() {
        state = CircuitBreakerState.HALF_OPEN;
        halfOpenTrades = 0;
        log.info("🟡 Circuit breaker → HALF_OPEN (recovery mode)");
        if (audit != null) {
            audit.log(AuditEventType.CIRCUIT_BREAKER_CHANGE, Map.of("new_state", state.name()));
        }
    }

    public void reset(String reason) {
        CircuitBreakerState old = state;
        state = CircuitBreakerState.CLOSED;
        tripReason = "";
        trippedAt = null;
        halfOpenTrades = 0;
        log.info("🟢 Circuit breaker RESET ({} → CLOSED): {}", old, reason);
        if (audit != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("new_state", state.name());
            d.put("old_state", old.name());
            d.put("reason", reason);
            audit.log(AuditEventType.CIRCUIT_BREAKER_CHANGE, d);
        }
    }

    public void resetDaily() {
        if (state == CircuitBreakerState.OPEN) reset("Auto-reset for new trading day");
        dailyPnl = 0.0;
        consecutiveLosses = 0;
        totalOrders = 0;
        failedOrders = 0;
        halfOpenTrades = 0;
        log.info("Circuit breaker daily counters reset");
    }

    public Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("state", state.name());
        m.put("trip_reason", tripReason);
        m.put("tripped_at", trippedAt == null ? null : trippedAt.toString());
        m.put("daily_pnl", dailyPnl);
        m.put("consecutive_losses", consecutiveLosses);
        m.put("total_orders", totalOrders);
        m.put("failed_orders", failedOrders);
        m.put("trip_count", tripCount);
        m.put("position_size_multiplier", positionSizeMultiplier());
        return m;
    }

    // ── Trigger checks (one method per trigger — SRP/OCP) ────

    private void checkDailyLossTrigger() {
        double maxLossPct = props.circuitBreaker().dailyLossPct();
        double maxLoss = totalCapital * (maxLossPct / 100.0);
        if (dailyPnl < -maxLoss) {
            trip(String.format("Daily loss limit exceeded: ₹%,.0f (limit: -₹%,.0f)", dailyPnl, maxLoss));
        }
    }

    private void checkConsecutiveLossTrigger() {
        int limit = props.circuitBreaker().consecutiveLosses();
        if (consecutiveLosses >= limit) {
            trip(String.format("Consecutive loss limit: %d losses in a row (limit: %d)",
                    consecutiveLosses, limit));
        }
    }

    private void checkDrawdownTrigger() {
        double current = totalCapital + dailyPnl;
        if (peakCapital > 0) {
            double drawdownPct = ((peakCapital - current) / peakCapital) * 100.0;
            // Fixed 5% drawdown cap (mirrors the Python default; not yet exposed via props).
            if (drawdownPct >= 5.0) {
                trip(String.format("Drawdown limit: %.1f%% from peak", drawdownPct));
            }
        }
    }

    private void checkErrorRateTrigger() {
        if (totalOrders >= 5) {
            double errorRate = (failedOrders * 100.0) / totalOrders;
            if (errorRate >= 50.0) {
                trip(String.format("Error rate too high: %.0f%% (%d/%d failed)",
                        errorRate, failedOrders, totalOrders));
            }
        }
    }

    private void recover() {
        reset("Half-open recovery successful");
    }
}
