package com.trendinvestorr.trading.paper;

import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.core.constants.OrderStatus;
import com.trendinvestorr.core.constants.ProductType;
import com.trendinvestorr.core.constants.TransactionType;
import com.trendinvestorr.core.config.TradingProperties;
import com.trendinvestorr.data.model.OrderRequest;
import com.trendinvestorr.data.model.OrderResult;
import com.trendinvestorr.data.model.Position;
import com.trendinvestorr.storage.service.AuditTrailService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory {@link ExecutionChannel} — simulates order fills against virtual
 * capital with configurable slippage. Production paper-trading default.
 *
 * <p>SRP: only order simulation + portfolio bookkeeping. Audit delegation to
 * {@link AuditTrailService}; slippage from {@link TradingProperties}.
 *
 * <p>Port of {@code trading/paper_trader.py}.
 */
@Service
public class PaperTrader implements ExecutionChannel {

    private static final Logger log = LoggerFactory.getLogger(PaperTrader.class);
    private static final double INITIAL_CAPITAL = 1_00_00_000.0;

    private final TradingProperties props;
    private final AuditTrailService audit;

    private volatile double cash = INITIAL_CAPITAL;
    /** symbol → Position */
    private final Map<String, Position> positions = new ConcurrentHashMap<>();
    private final List<OrderResult> orderHistory = new ArrayList<>();
    private final List<Map<String, Object>> closedTrades = new ArrayList<>();

    private volatile double dailyRealizedPnl = 0.0;
    private volatile int dailyTrades = 0;
    private volatile int winningTrades = 0;
    private volatile int losingTrades = 0;

    public PaperTrader(TradingProperties props, AuditTrailService audit) {
        this.props = props;
        this.audit = audit;
    }

    @Override
    public synchronized OrderResult placeOrder(OrderRequest order) {
        try {
            double slip = order.price() * (props.slippagePct() / 100.0);
            fill(order, slip);
            return successResult(order);
        } catch (Exception e) {
            log.error("PaperTrader fill error for {}: {}", order.symbol(), e.getMessage());
            return new OrderResult(false, "", "", OrderStatus.REJECTED, 0, 0.0,
                    "Fill error: " + e.getMessage(), true, Instant.now());
        }
    }

    private void fill(OrderRequest order, double slippage) {
        double fillPrice = order.transactionType() == TransactionType.BUY
                ? order.price() + slippage
                : order.price() - slippage;
        int qty = order.quantity();
        if (qty <= 0) return;

        if (order.transactionType() == TransactionType.BUY) {
            cash -= fillPrice * qty;
            Position existing = positions.get(order.symbol());
            if (existing == null) {
                positions.put(order.symbol(), new Position(
                        order.symbol(), order.token(), qty, fillPrice, fillPrice,
                        order.productType(), true, Instant.now()));
            } else {
                int newQty = existing.quantity() + qty;
                double newAvg = ((existing.avgBuyPrice() * existing.quantity()) + (fillPrice * qty)) / newQty;
                positions.put(order.symbol(), new Position(
                        order.symbol(), existing.token(), newQty, newAvg, fillPrice,
                        existing.productType(), true, existing.openedAt()));
            }
        } else {
            // SELL — close (or reduce) the position, realize P&L.
            Position p = positions.get(order.symbol());
            if (p == null) return;
            double pnl = (fillPrice - p.avgBuyPrice()) * qty;
            cash += fillPrice * qty;
            dailyRealizedPnl += pnl;
            dailyTrades++;
            if (pnl >= 0) winningTrades++; else losingTrades++;

            log.info("📝 Paper SELL {} {} @ ₹{:.2f} | P&L ₹{:+.2f}", order.symbol(), qty, fillPrice, pnl);
            Map<String, Object> closed = new LinkedHashMap<>();
            closed.put("symbol", order.symbol());
            closed.put("qty", qty);
            closed.put("buy_price", p.avgBuyPrice());
            closed.put("sell_price", fillPrice);
            closed.put("pnl", pnl);
            closedTrades.add(closed);

            int remaining = p.quantity() - qty;
            if (remaining <= 0) positions.remove(order.symbol());
            else positions.put(order.symbol(), new Position(
                    order.symbol(), p.token(), remaining, p.avgBuyPrice(), fillPrice,
                    p.productType(), true, p.openedAt()));

            if (audit != null) audit.log(AuditEventType.ORDER_FILLED,
                    Map.of("symbol", order.symbol(), "pnl", pnl, "is_paper", true));
        }
    }

    private OrderResult successResult(OrderRequest order) {
        OrderResult r = new OrderResult(true, "PAPER-" + UUID.randomUUID(), "",
                OrderStatus.FILLED, order.quantity(), order.price(),
                "Paper fill simulated", true, Instant.now());
        orderHistory.add(r);
        return r;
    }

    @Override
    public List<Position> positions() {
        return List.copyOf(positions.values());
    }

    @Override
    public void updatePrices(Map<String, Double> prices) {
        if (prices == null) return;
        for (Map.Entry<String, Double> e : prices.entrySet()) {
            Position p = positions.get(e.getKey());
            if (p != null && e.getValue() > 0) {
                positions.put(e.getKey(), new Position(
                        p.symbol(), p.token(), p.quantity(), p.avgBuyPrice(),
                        e.getValue(), p.productType(), true, p.openedAt()));
            }
        }
    }

    @Override
    public synchronized List<OrderResult> squareOffIntraday() {
        List<OrderResult> out = new ArrayList<>();
        for (Position p : List.copyOf(positions.values())) {
            if (p.productType() != ProductType.INTRADAY) continue;
            OrderRequest sell = new OrderRequest(p.symbol(), p.token(), TransactionType.SELL,
                    p.quantity(), com.trendinvestorr.core.constants.OrderType.MARKET,
                    ProductType.INTRADAY, com.trendinvestorr.core.constants.OrderVariety.NORMAL,
                    p.currentPrice(), 0.0, com.trendinvestorr.core.constants.Exchange.NSE, null);
            out.add(placeOrder(sell));
        }
        log.info("🔔 Square-off: closed {} intraday positions", out.size());
        if (audit != null) audit.log(AuditEventType.POSITION_SQUARED_OFF,
                Map.of("closed", out.size(), "is_paper", true));
        return out;
    }

    @Override
    public double dailyRealizedPnl() { return dailyRealizedPnl; }

    @Override
    public double dailyUnrealizedPnl() {
        return positions.values().stream().mapToDouble(Position::unrealizedPnl).sum();
    }

    @Override
    public synchronized void resetDaily() {
        dailyRealizedPnl = 0.0;
        dailyTrades = 0;
        winningTrades = 0;
        losingTrades = 0;
        closedTrades.clear();
        log.info("Paper trader daily counters reset");
    }

    @Override
    public synchronized Map<String, Object> stats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("cash", cash);
        m.put("portfolio_value", portfolioValue());
        m.put("open_positions", positions.size());
        m.put("daily_realized_pnl", dailyRealizedPnl);
        m.put("daily_unrealized_pnl", dailyUnrealizedPnl());
        m.put("daily_trades", dailyTrades);
        m.put("winning_trades", winningTrades);
        m.put("losing_trades", losingTrades);
        return m;
    }

    public double portfolioValue() {
        return cash + positions.values().stream().mapToDouble(Position::currentValue).sum();
    }
}
