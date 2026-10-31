package com.trendinvestorr.strategy.algorithm;

import com.trendinvestorr.data.model.Observation;

import java.time.Instant;
import java.util.HashMap;
import java.util.Map;

/**
 * Represents a trade currently being managed by an algorithm.
 * Created when the algorithm decides to enter, updated on each tick,
 * closed when exit conditions are met.
 *
 * <p>Mutable (price/P&L update on every tick) — modelled as a class, not a record.
 * Port of {@code strategies.base.ActiveTrade}.
 */
public class ActiveTrade {
    private final String symbol;
    private final String token;
    private final String algorithmName;
    private final String direction;            // "BUY" or "SELL"
    private final double entryPrice;
    private final Instant entryTime;
    private int quantity;

    // Risk levels set by the algorithm
    private double stopLoss;
    private double targetPrice;
    private double trailingStop;

    // Trade management state
    private double highestPrice;               // highest since entry (for trailing)
    private double lowestPrice;                // lowest since entry
    private double currentPrice;
    private int ticksSinceEntry;
    private int candlesSinceEntry;

    // P&L tracking
    private double unrealizedPnl;
    private double unrealizedPnlPct;

    // Metadata
    private Observation entryObservation;
    private final Map<String, Object> metadata;

    public ActiveTrade(String symbol, String token, String algorithmName, String direction,
                       double entryPrice, Instant entryTime, int quantity,
                       double stopLoss, double targetPrice, double trailingStop,
                       Observation entryObservation, Map<String, Object> metadata) {
        this.symbol = symbol;
        this.token = token;
        this.algorithmName = algorithmName;
        this.direction = direction;
        this.entryPrice = entryPrice;
        this.entryTime = entryTime;
        this.quantity = quantity;
        this.stopLoss = stopLoss;
        this.targetPrice = targetPrice;
        this.trailingStop = trailingStop;
        this.highestPrice = entryPrice;
        this.lowestPrice = Double.POSITIVE_INFINITY;
        this.currentPrice = entryPrice;
        this.entryObservation = entryObservation;
        this.metadata = metadata == null ? new HashMap<>() : new HashMap<>(metadata);
    }

    /** Update with the latest tick price and recalc P&L. */
    public void updatePrice(double price) {
        this.currentPrice = price;
        this.ticksSinceEntry++;
        if (price > highestPrice) highestPrice = price;
        if (price < lowestPrice) lowestPrice = price;

        double diff;
        if ("BUY".equalsIgnoreCase(direction)) {
            diff = price - entryPrice;
        } else {
            diff = entryPrice - price;
        }
        this.unrealizedPnl = diff * quantity;
        this.unrealizedPnlPct = entryPrice == 0 ? 0.0 : (diff / entryPrice) * 100.0;
    }

    public String symbol() { return symbol; }
    public String token() { return token; }
    public String algorithmName() { return algorithmName; }
    public String direction() { return direction; }
    public double entryPrice() { return entryPrice; }
    public Instant entryTime() { return entryTime; }
    public int quantity() { return quantity; }
    public double stopLoss() { return stopLoss; }
    public double targetPrice() { return targetPrice; }
    public double trailingStop() { return trailingStop; }
    public double highestPrice() { return highestPrice; }
    public double lowestPrice() { return lowestPrice; }
    public double currentPrice() { return currentPrice; }
    public int ticksSinceEntry() { return ticksSinceEntry; }
    public int candlesSinceEntry() { return candlesSinceEntry; }
    public double unrealizedPnl() { return unrealizedPnl; }
    public double unrealizedPnlPct() { return unrealizedPnlPct; }
    public Observation entryObservation() { return entryObservation; }
    public Map<String, Object> metadata() { return metadata; }

    public void setQuantity(int quantity) { this.quantity = quantity; }
    public void setStopLoss(double stopLoss) { this.stopLoss = stopLoss; }
    public void setTargetPrice(double targetPrice) { this.targetPrice = targetPrice; }
    public void setTrailingStop(double trailingStop) { this.trailingStop = trailingStop; }
    public void setEntryObservation(Observation obs) { this.entryObservation = obs; }
    public void incrementCandlesSinceEntry() { this.candlesSinceEntry++; }
}
