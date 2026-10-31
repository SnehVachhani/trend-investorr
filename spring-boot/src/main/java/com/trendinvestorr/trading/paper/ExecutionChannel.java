package com.trendinvestorr.trading.paper;

import com.trendinvestorr.data.model.OrderRequest;
import com.trendinvestorr.data.model.OrderResult;
import com.trendinvestorr.data.model.Position;

import java.util.List;
import java.util.Map;

/**
 * Execution channel abstraction. OrderManager depends on this interface, not a
 * concrete trader — so a real broker adapter can be plugged in alongside the
 * paper implementation without touching the orchestrator (DIP/OCP).
 *
 * <p>Port of the implicit interface shared by {@code PaperTrader} and the
 * live-execution path in {@code trading/order_manager.py}.
 */
public interface ExecutionChannel {

    /** Place an order through this channel (paper / live / shadow). */
    OrderResult placeOrder(OrderRequest order);

    /** Current open positions held by this channel. */
    List<Position> positions();

    /** Update mark prices for live (unrealized) P&L. */
    void updatePrices(Map<String, Double> prices);

    /** Square off every intraday position — returns one result per closed position. */
    List<OrderResult> squareOffIntraday();

    /** Realized P&L since the last daily reset. */
    double dailyRealizedPnl();

    /** Unrealized P&L at current mark prices. */
    double dailyUnrealizedPnl();

    /** Reset daily counters for a new trading day. */
    void resetDaily();

    /** Channel stat snapshot for monitoring. */
    Map<String, Object> stats();
}
