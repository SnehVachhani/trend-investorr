package com.trendinvestorr.trading.risk;

import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.core.constants.CircuitBreakerState;
import com.trendinvestorr.core.constants.MarketConstants;
import com.trendinvestorr.core.constants.ProductType;
import com.trendinvestorr.core.constants.TransactionType;
import com.trendinvestorr.core.config.TradingProperties;
import com.trendinvestorr.data.model.OrderRequest;
import com.trendinvestorr.data.model.Position;
import com.trendinvestorr.data.model.RiskDecision;
import com.trendinvestorr.data.model.TechnicalIndicators;
import com.trendinvestorr.storage.service.AuditTrailService;
import com.trendinvestorr.trading.circuit.CircuitBreaker;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalTime;
import java.time.ZonedDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Multi-gate risk validation that every order MUST pass before execution.
 * The last line of defense between signal generation and real money.
 *
 * <p>Gates (in order): circuit-breaker → market hours → position limits →
 * daily loss → capital allocation → position sizing → order-value bounds →
 * stop-loss / target. Each gate is a single-responsibility private method
 * returning {@code null} to pass or a rejection {@link RiskDecision}.
 *
 * <p>Depends on {@link CircuitBreaker} and {@link AuditTrailService}
 * abstractions, not concrete wiring (DIP).
 *
 * <p>Port of {@code trading/risk_manager.py}.
 */
@Service
public class RiskManager {

    private static final Logger log = LoggerFactory.getLogger(RiskManager.class);

    private final TradingProperties props;
    private final CircuitBreaker circuitBreaker;
    private final AuditTrailService audit;

    // Paper-trading capital (runtime-tunable in future via TradingProperties).
    private static final double TOTAL_CAPITAL = 1_00_00_000.0;

    private volatile double dailyRealizedPnl = 0.0;
    private volatile double dailyUnrealizedPnl = 0.0;
    private volatile int dailyTrades = 0;

    public RiskManager(TradingProperties props, CircuitBreaker circuitBreaker, AuditTrailService audit) {
        this.props = props;
        this.circuitBreaker = circuitBreaker;
        this.audit = audit;
    }

    /** Run all gates; never throws — returns a {@link RiskDecision}. */
    public RiskDecision evaluate(OrderRequest order, List<Position> positions,
                                 String sector, TechnicalIndicators indicators) {
        try {
            // Gate 0: circuit breaker must be closed or half-open.
            if (circuitBreaker.state() == CircuitBreakerState.OPEN) {
                return reject(order, "Circuit breaker OPEN — trading halted");
            }

            RiskDecision r;
            if ((r = checkMarketHours(order)) != null) return r;
            if ((r = checkPositionLimits(order, positions)) != null) return r;
            if ((r = checkDailyLossLimit()) != null) return r;

            double maxValue = maxTradeValue(positions);
            if (maxValue <= 0) return reject(order, "Capital fully deployed — no allocation available");

            int quantity = positionSize(order, maxValue, indicators);
            if (quantity < 1) return reject(order, "Calculated quantity " + quantity + " below minimum 1");

            double orderValue = order.price() > 0 ? order.price() * quantity : 0;
            if (orderValue > 0 && orderValue < 5_000.0) {
                return reject(order, String.format(
                        "Order value ₹%,.0f below minimum ₹%,.0f", orderValue, 5_000.0));
            }

            double stopLoss = calculateStopLoss(order, indicators);
            double target = calculateTarget(order);

            dailyTrades++;
            if (audit != null) {
                Map<String, Object> d = new LinkedHashMap<>();
                d.put("symbol", order.symbol());
                d.put("action", "risk_approved");
                d.put("quantity", quantity);
                d.put("price", order.price());
                d.put("stop_loss", stopLoss);
                d.put("target", target);
                d.put("daily_trades", dailyTrades);
                audit.log(AuditEventType.ORDER_PLACED, d);
            }
            log.info("✅ Risk APPROVED: {} | qty={} | SL=₹{:.2f} | T=₹{:.2f} | max_value=₹{:.0f}",
                    order.symbol(), quantity, stopLoss, target, maxValue);
            return RiskDecision.approve(quantity, maxValue, stopLoss, target,
                    String.format("Approved: %d shares, SL=₹%.2f, T=₹%.2f", quantity, stopLoss, target));
        } catch (Exception e) {
            log.error("Risk evaluation error for {}: {}", order.symbol(), e.getMessage());
            return reject(order, "Risk evaluation error: " + e.getMessage());
        }
    }

    // ── Gates (each one rule — SRP) ──────────────────────────

    private RiskDecision checkMarketHours(OrderRequest order) {
        LocalTime now = LocalTime.of(9, 10);
        LocalTime current = ZonedDateTime.now(MarketConstants.IST).toLocalTime();
        if (current.isBefore(now) || current.isAfter(MarketConstants.MARKET_CLOSE)) {
            if (order.productType() == ProductType.INTRADAY) {
                return reject(order, String.format(
                        "Market closed — intraday not allowed (now %s, market %s-%s)",
                        current, MarketConstants.MARKET_OPEN, MarketConstants.MARKET_CLOSE));
            }
        }
        return null;
    }

    private RiskDecision checkPositionLimits(OrderRequest order, List<Position> positions) {
        if (order.transactionType() == TransactionType.SELL) return null;
        if (positions.size() >= props.maxOpenPositions()) {
            return reject(order, "Max open positions reached (" + props.maxOpenPositions() + ")");
        }
        long stockPositions = positions.stream().filter(p -> p.symbol().equals(order.symbol())).count();
        if (stockPositions >= 1) {
            return reject(order, "Already holding position in " + order.symbol());
        }
        return null;
    }

    private RiskDecision checkDailyLossLimit() {
        double total = dailyRealizedPnl + dailyUnrealizedPnl;
        double maxLoss = TOTAL_CAPITAL * (props.maxDailyLossPct() / 100.0);
        if (total < -maxLoss) {
            String msg = String.format("Daily loss limit hit: ₹%,.0f (limit -₹%,.0f = %s%%)",
                    total, maxLoss, props.maxDailyLossPct());
            log.warn("🛑 {}", msg);
            if (audit != null) {
                Map<String, Object> d = new LinkedHashMap<>();
                d.put("limit", "daily_loss");
                d.put("pnl", total);
                d.put("max_loss", maxLoss);
                audit.log(AuditEventType.RISK_LIMIT_HIT, d);
            }
            return RiskDecision.reject(msg);
        }
        return null;
    }

    private double maxTradeValue(List<Position> positions) {
        double deployed = positions.stream().mapToDouble(Position::investedValue).sum();
        double maxDeployed = TOTAL_CAPITAL * 0.70;          // 70% deployed cap
        double available = maxDeployed - deployed;
        double perTradeMax = TOTAL_CAPITAL * (props.maxCapitalPerTradePct() / 100.0);
        return Math.min(Math.min(available, perTradeMax), 5_00_000.0);
    }

    private int positionSize(OrderRequest order, double max, TechnicalIndicators ind) {
        if (order.price() <= 0) return 0;
        int byCapital = (int) (max / order.price());
        double stopDistance = (ind != null && ind.atr14() > 0)
                ? ind.atr14() * 2.0
                : order.price() * (props.defaultStopLossPct() / 100.0);
        double riskCapital = TOTAL_CAPITAL * 0.01;           // 1% risk per trade
        int byRisk = stopDistance > 0 ? (int) (riskCapital / stopDistance) : byCapital;
        int qty = Math.min(byCapital, byRisk);
        return Math.max(1, Math.min(qty, 10_000));
    }

    private double calculateStopLoss(OrderRequest order, TechnicalIndicators ind) {
        if (order.triggerPrice() > 0) return order.triggerPrice();
        if (order.price() <= 0) return 0.0;
        if (ind != null && ind.atr14() > 0) {
            return order.transactionType() == TransactionType.BUY
                    ? order.price() - (ind.atr14() * 2.0)
                    : order.price() + (ind.atr14() * 2.0);
        }
        return order.transactionType() == TransactionType.BUY
                ? order.price() * (1 - props.defaultStopLossPct() / 100.0)
                : order.price() * (1 + props.defaultStopLossPct() / 100.0);
    }

    private double calculateTarget(OrderRequest order) {
        if (order.price() <= 0) return 0.0;
        return order.transactionType() == TransactionType.BUY
                ? order.price() * (1 + props.defaultTargetPct() / 100.0)
                : order.price() * (1 - props.defaultTargetPct() / 100.0);
    }

    private RiskDecision reject(OrderRequest order, String reason) {
        log.warn("❌ Risk REJECTED: {} — {}", order.symbol(), reason);
        if (audit != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("symbol", order.symbol());
            d.put("reason", reason);
            audit.log(AuditEventType.ORDER_REJECTED, d);
        }
        return RiskDecision.reject(reason);
    }

    // ── P&L & daily reset ─────────────────────────────────────

    public void updateDailyPnl(double realized, double unrealized) {
        this.dailyRealizedPnl = realized;
        this.dailyUnrealizedPnl = unrealized;
    }

    public void recordTradePnl(double pnl) {
        this.dailyRealizedPnl += pnl;
        this.dailyTrades++;
    }

    public void resetDaily() {
        dailyRealizedPnl = 0.0;
        dailyUnrealizedPnl = 0.0;
        dailyTrades = 0;
        log.info("Risk manager daily counters reset");
    }

    public Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("daily_realized_pnl", dailyRealizedPnl);
        m.put("daily_unrealized_pnl", dailyUnrealizedPnl);
        m.put("daily_total_pnl", dailyRealizedPnl + dailyUnrealizedPnl);
        m.put("daily_trades", dailyTrades);
        Map<String, Object> cfg = new LinkedHashMap<>();
        cfg.put("total_capital", TOTAL_CAPITAL);
        cfg.put("max_per_trade_pct", props.maxCapitalPerTradePct());
        cfg.put("max_daily_loss_pct", props.maxDailyLossPct());
        cfg.put("max_positions", props.maxOpenPositions());
        m.put("config", cfg);
        return m;
    }
}
