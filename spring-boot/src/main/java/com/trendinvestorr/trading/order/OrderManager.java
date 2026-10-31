package com.trendinvestorr.trading.order;

import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.core.constants.OrderStatus;
import com.trendinvestorr.core.constants.ProductType;
import com.trendinvestorr.core.constants.TransactionType;
import com.trendinvestorr.core.exceptions.CircuitBreakerOpenError;
import com.trendinvestorr.data.model.AnalysisResult;
import com.trendinvestorr.data.model.OrderRequest;
import com.trendinvestorr.data.model.OrderResult;
import com.trendinvestorr.data.model.Position;
import com.trendinvestorr.data.model.RiskDecision;
import com.trendinvestorr.data.model.Signal;
import com.trendinvestorr.data.model.TechnicalIndicators;
import com.trendinvestorr.storage.service.AuditTrailService;
import com.trendinvestorr.trading.circuit.CircuitBreaker;
import com.trendinvestorr.trading.paper.ExecutionChannel;
import com.trendinvestorr.trading.risk.RiskManager;
import com.trendinvestorr.core.constants.OrderType;
import com.trendinvestorr.core.constants.OrderVariety;
import com.trendinvestorr.core.constants.Exchange;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Top-level order orchestrator. Wires signal → circuit-breaker check → order
 * build → risk evaluation → position-size adjustment → execution channel →
 * result recording.
 *
 * <p><b>SOLID:</b> Depends on {@link ExecutionChannel} abstraction (not the
 * concrete {@code PaperTrader}), so a live broker adapter can be plugged in
 * for LIVE/DUAL modes without changing this class (DIP/OCP). One method per
 * pipeline gate (SRP).
 *
 * <p>Port of {@code trading/order_manager.py}.
 */
@Service
public class OrderManager {

    private static final Logger log = LoggerFactory.getLogger(OrderManager.class);

    public enum TradingMode { PAPER, LIVE, DUAL }

    private final RiskManager riskManager;
    private final CircuitBreaker circuitBreaker;
    private final ExecutionChannel channel;
    private final AuditTrailService audit;

    private volatile TradingMode mode = TradingMode.PAPER;
    private int ordersPlaced = 0;
    private int ordersFilled = 0;
    private int ordersRejected = 0;

    public OrderManager(RiskManager riskManager, CircuitBreaker circuitBreaker,
                        ExecutionChannel channel, AuditTrailService audit) {
        this.riskManager = riskManager;
        this.circuitBreaker = circuitBreaker;
        this.channel = channel;
        this.audit = audit;
        log.info("OrderManager initialized: mode=PAPER");
    }

    /** Run the full pipeline for a validated signal. */
    public OrderResult executeSignal(Signal signal, AnalysisResult analysis, String sector) {
        try {
            // Gate 1: circuit breaker.
            try {
                circuitBreaker.check();
            } catch (CircuitBreakerOpenError e) {
                log.warn("Order blocked by circuit breaker: {}", signal.symbol());
                return null;
            }

            // Gate 2: analysis must be actionable.
            if (!analysis.isActionable()) {
                log.debug("Signal {} not actionable: {}", signal.symbol(), analysis.action());
                return null;
            }

            OrderRequest order = buildOrder(signal, analysis);
            List<Position> positions = channel.positions();
            TechnicalIndicators ind = indicatorsFrom(signal);
            RiskDecision rd = riskManager.evaluate(order, positions, sector, ind);

            if (!rd.approved()) {
                ordersRejected++;
                circuitBreaker.recordOrderFailure();
                log.info("Order rejected by risk manager: {}", rd.reason());
                return new OrderResult(false, "", "", OrderStatus.REJECTED, 0, 0.0,
                        rd.reason(), mode != TradingMode.LIVE, java.time.Instant.now());
            }

            // Adjust quantity for circuit breaker state.
            double cbMult = circuitBreaker.positionSizeMultiplier();
            int adjustedQty = Math.max(1, (int) (rd.quantity() * cbMult));
            OrderRequest sized = new OrderRequest(order.symbol(), order.token(),
                    order.transactionType(), adjustedQty, order.orderType(), order.productType(),
                    order.variety(), order.price(), rd.stopLossPrice() > 0 ? rd.stopLossPrice() : 0.0,
                    order.exchange(), order.signalId());

            OrderResult result = channel.placeOrder(sized);
            if (result.success()) {
                ordersPlaced++;
                ordersFilled++;
                if (audit != null) {
                    Map<String, Object> d = new LinkedHashMap<>();
                    d.put("symbol", signal.symbol());
                    d.put("side", order.transactionType());
                    d.put("quantity", result.filledQty());
                    d.put("fill_price", result.avgFillPrice());
                    d.put("mode", mode);
                    d.put("order_id", result.orderId());
                    d.put("signal_score", signal.score());
                    d.put("analysis_confidence", analysis.confidence());
                    d.put("analyzer", analysis.analyzerName());
                    audit.log(AuditEventType.ORDER_FILLED, d);
                }
            } else {
                ordersRejected++;
                circuitBreaker.recordOrderFailure();
            }
            return result;
        } catch (Exception e) {
            log.error("Order execution error for {}: {}", signal.symbol(), e.getMessage());
            circuitBreaker.recordOrderFailure();
            return new OrderResult(false, "", "", OrderStatus.REJECTED, 0, 0.0,
                    "Execution error: " + e.getMessage(), mode != TradingMode.LIVE, java.time.Instant.now());
        }
    }

    private OrderRequest buildOrder(Signal signal, AnalysisResult analysis) {
        TransactionType tx = signal.signalType() == com.trendinvestorr.core.constants.SignalType.BUY
                ? TransactionType.BUY : TransactionType.SELL;
        return new OrderRequest(signal.symbol(), signal.token(), tx, 0, OrderType.MARKET,
                ProductType.INTRADAY, OrderVariety.NORMAL, signal.price(), 0.0,
                Exchange.NSE, null);
    }

    @SuppressWarnings("unchecked")
    private TechnicalIndicators indicatorsFrom(Signal signal) {
        Object o = signal.metadata() == null ? null : signal.metadata().get("precomputed_indicators");
        return (o instanceof TechnicalIndicators ti) ? ti : null;
    }

    public List<OrderResult> squareOffIntraday() {
        List<OrderResult> results = channel.squareOffIntraday();
        if (audit != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("action", "intraday_square_off");
            d.put("mode", mode);
            d.put("positions_closed", results.size());
            audit.log(AuditEventType.SCHEDULER_TASK, d);
        }
        return results;
    }

    public void updatePrices(Map<String, Double> prices) {
        channel.updatePrices(prices);
        riskManager.updateDailyPnl(channel.dailyRealizedPnl(), channel.dailyUnrealizedPnl());
    }

    public void setMode(TradingMode mode) {
        TradingMode old = this.mode;
        this.mode = mode;
        log.info("Trading mode changed: {} → {}", old, mode);
        if (audit != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("setting", "trading_mode");
            d.put("old", old);
            d.put("new", mode);
            audit.log(AuditEventType.CONFIG_CHANGE, d);
        }
    }

    public TradingMode mode() { return mode; }

    public Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("mode", mode);
        m.put("orders_placed", ordersPlaced);
        m.put("orders_filled", ordersFilled);
        m.put("orders_rejected", ordersRejected);
        double denom = Math.max(ordersPlaced, 1);
        m.put("fill_rate", String.format("%.0f%%", (ordersFilled / denom) * 100.0));
        m.put("channel_stats", channel.stats());
        m.put("circuit_breaker", circuitBreaker.stats());
        return m;
    }
}
