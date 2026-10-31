package com.trendinvestorr.core.exceptions;

/**
 * WebSocket connection error (disconnect, timeout, etc.).
 * Port of {@code core.exceptions.WebSocketError}.
 */
public class WebSocketError extends TrendInvestorrError {
    public WebSocketError(String message) { super(message); }
    public WebSocketError(String message, Throwable cause) { super(message, cause); }
}
