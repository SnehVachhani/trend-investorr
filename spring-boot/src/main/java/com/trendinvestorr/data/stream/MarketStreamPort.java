package com.trendinvestorr.data.stream;

import com.trendinvestorr.core.constants.FeedMode;
import com.trendinvestorr.data.model.Tick;

import java.util.List;
import java.util.function.Consumer;

/**
 * Real-time market-data stream port — abstracts the SmartAPI WebSocket V2 tick feed.
 *
 * <p>Scanners/aggregators register a {@link Consumer} of {@link Tick} and never know
 * whether ticks come from a live WSS, a replay, or a test stub. This is the seam that
 * makes the engine unit-testable without a broker connection.
 *
 * <p>Port of {@code data.streams.websocket_manager.WebSocketManager}.
 */
public interface MarketStreamPort {

    void onTick(Consumer<Tick> callback);

    void subscribe(List<String> tokens) throws com.trendinvestorr.core.exceptions.WebSocketError;

    void unsubscribe(List<String> tokens);

    void start();

    void stop();

    /** Most recent {@code count} ticks across all symbols (ring buffer). */
    List<Tick> latestTicks(int count);

    default Tick latestTick(String token) {
        return latestTicks(1).stream()
                .filter(t -> token.equals(t.token()))
                .findFirst().orElse(null);
    }

    boolean isConnected();

    boolean isRunning();

    int subscribedCount();

    FeedMode feedMode();
}
