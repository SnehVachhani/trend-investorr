package com.trendinvestorr.core.constants;

/**
 * Auditable, security-relevant and lifecycle events written to the {@code audit_log} table.
 * Port of {@code core.constants.AuditEventType}.
 */
public enum AuditEventType {
    // Auth
    LOGIN_SUCCESS,
    LOGIN_FAILURE,
    SESSION_EXPIRED,
    SESSION_RENEWED,

    // Trading
    ORDER_PLACED,
    ORDER_FILLED,
    ORDER_REJECTED,
    ORDER_CANCELLED,
    POSITION_SQUARED_OFF,

    // Signals
    SIGNAL_GENERATED,
    SIGNAL_VALIDATED,
    SIGNAL_EXPIRED,

    // Risk
    CIRCUIT_BREAKER_OPENED,
    CIRCUIT_BREAKER_CLOSED,
    CIRCUIT_BREAKER_CHANGE,
    RISK_LIMIT_HIT,

    // Config
    CONFIG_CHANGED,
    CONFIG_CHANGE,
    VAULT_ACCESSED,

    // System
    SYSTEM_START,
    SYSTEM_STARTUP,
    SYSTEM_SHUTDOWN,
    ERROR,
    SCHEDULER_TASK
}
