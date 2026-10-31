package com.trendinvestorr.strategy.algorithm;

import com.trendinvestorr.core.constants.SignalSource;
import com.trendinvestorr.core.constants.SignalStrength;
import com.trendinvestorr.core.constants.SignalType;
import com.trendinvestorr.data.model.Observation;
import com.trendinvestorr.data.model.Signal;
import com.trendinvestorr.data.model.Tick;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * Abstract base class for all trading algorithms. Port of {@code strategies.base.TradingAlgorithm}.
 *
 * <p>Each TradingAlgorithm is a self-contained strategy with:
 * <ul>
 *   <li>Entry conditions (when to open)</li>
 *   <li>Exit conditions (profit, stop, time, indicator)</li>
 *   <li>Trade management (trailing stops, partial exits, scaling)</li>
 *   <li>Position tracking (what it currently holds, entry price/time)</li>
 *   <li>State machine (IDLE → WATCHING → IN_TRADE → EXITED)</li>
 * </ul>
 *
 * <p>Lifecycle:
 * <ol>
 *   <li>Scanners emit observations</li>
 *   <li>AlgorithmManager routes relevant observations to this algorithm</li>
 *   <li>Algorithm stores observations it cares about ({@link #onObservation})</li>
 *   <li>On each tick, algorithm updates active trades ({@link #onTick})</li>
 *   <li>Periodically, algorithm checks if entry conditions met ({@link #checkEntries})</li>
 *   <li>For active trades, algorithm checks exit conditions ({@link #checkExits})</li>
 * </ol>
 */
public abstract class TradingAlgorithm {

    private final String name;
    private final boolean enabled;

    // Active trades managed by this algorithm: key = symbol
    protected final Map<String, ActiveTrade> activeTrades = new LinkedHashMap<>();

    // Stats
    protected int totalEntries;
    protected int totalExits;
    protected int winningExits;
    protected int losingExits;

    protected TradingAlgorithm(String name, boolean enabled) {
        this.name = name;
        this.enabled = enabled;
    }

    public String name() {
        return name;
    }

    public boolean enabled() {
        return enabled;
    }

    public Map<String, ActiveTrade> activeTrades() {
        return activeTrades;
    }

    public boolean hasActiveTrades() {
        return !activeTrades.isEmpty();
    }

    // ── Abstract methods (must implement) ─────────────────────────

    /**
     * Which scanner sources does this algorithm care about?
     * The AlgorithmManager only routes matching observations to {@link #onObservation}.
     */
    public abstract List<SignalSource> interestedSources();

    /**
     * Process an incoming scanner observation. Store relevant data — this does
     * NOT trigger a trade, it just feeds information to the algorithm.
     */
    public abstract void onObservation(Observation obs);

    /**
     * Update active trades with new tick data. Called on EVERY tick (not just when
     * scanners fire). Use this to update prices, track trailing stops, count time.
     */
    public abstract void onTick(Tick tick);

    /**
     * Check if any entry conditions are met. Called periodically.
     * Returns a list of EntrySignal objects for stocks that meet all entry criteria.
     * This only PROPOSES entries — the AlgorithmManager still routes them through
     * the RiskManager before executing.
     */
    public abstract List<EntrySignal> checkEntries();

    /**
     * Check if any active trades should be exited. Called per tick.
     * Returns a list of ExitSignal objects for trades that should be closed.
     */
    public abstract List<ExitSignal> checkExits();

    // ── Common methods (shared by all algorithms) ────────────────

    /**
     * Called by AlgorithmManager after a trade is successfully placed.
     */
    public void registerTrade(ActiveTrade trade) {
        activeTrades.put(trade.symbol(), trade);
        totalEntries++;
    }

    /**
     * Remove a trade from active tracking after it's closed.
     */
    public ActiveTrade closeTrade(String symbol, ExitReason reason) {
        ActiveTrade trade = activeTrades.remove(symbol);
        if (trade != null) {
            totalExits++;
            if (trade.unrealizedPnl() >= 0) winningExits++;
            else losingExits++;
        }
        return trade;
    }

    public boolean isInTrade(String symbol) {
        return activeTrades.containsKey(symbol);
    }

    /**
     * Reset for a new trading day. Default: clear all active trades (intraday assumption).
     * Override in subclasses if the algorithm holds overnight state.
     */
    public void resetDaily() {
        activeTrades.clear();
    }

    /**
     * Convert an EntrySignal to a Signal for the existing execution pipeline
     * (Analyzer → RiskManager → OrderManager).
     */
    public Signal entryToSignal(EntrySignal entry) {
        SignalType type = "SELL".equalsIgnoreCase(entry.direction())
                ? SignalType.SELL : SignalType.BUY;

        SignalStrength strength;
        if (entry.confidence() >= 0.80) strength = SignalStrength.EXTREME;
        else if (entry.confidence() >= 0.60) strength = SignalStrength.STRONG;
        else if (entry.confidence() >= 0.40) strength = SignalStrength.MODERATE;
        else strength = SignalStrength.WEAK;

        SignalSource primary = SignalSource.MOMENTUM;
        if (entry.triggeringObservations() != null && !entry.triggeringObservations().isEmpty()) {
            primary = entry.triggeringObservations().get(0).source();
        }

        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("algorithm", entry.algorithmName());
        meta.put("stop_loss", entry.stopLoss());
        meta.put("target_price", entry.targetPrice());
        meta.put("entry_reason", entry.reason());
        meta.put("algorithm_confidence", Math.round(entry.confidence() * 1000.0) / 1000.0);
        if (entry.metadata() != null) meta.putAll(entry.metadata());

        return new Signal(
                entry.symbol(), entry.token(), type, strength,
                entry.confidence(), entry.price(), primary,
                0.0,
                meta
        );
    }

    public Map<String, Object> stats() {
        double winRate = totalExits > 0 ? (winningExits * 100.0 / totalExits) : 0.0;
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("name", name);
        s.put("enabled", enabled);
        s.put("active_trades", activeTrades.size());
        s.put("total_entries", totalEntries);
        s.put("total_exits", totalExits);
        s.put("winning_exits", winningExits);
        s.put("losing_exits", losingExits);
        s.put("win_rate", Math.round(winRate * 10.0) / 10.0);
        return s;
    }

    @Override
    public String toString() {
        return getClass().getSimpleName() + "{name=" + name
                + ", active=" + activeTrades.size()
                + ", entries=" + totalEntries + "}";
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof TradingAlgorithm that)) return false;
        return Objects.equals(name, that.name);
    }

    @Override
    public int hashCode() {
        return Objects.hash(name);
    }
}
