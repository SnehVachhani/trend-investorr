"""
Trend-Investorr: Custom Exception Hierarchy

All application-specific exceptions inherit from TrendInvestorrError.
This allows catching all app errors with a single except clause while
still being able to handle specific error types granularly.
"""


class TrendInvestorrError(Exception):
    """Base exception for all Trend-Investorr errors."""

    def __init__(self, message: str = "", details: dict | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


# ── Authentication & Session ─────────────────────────────────


class AuthenticationError(TrendInvestorrError):
    """Failed to authenticate with broker API (invalid credentials, TOTP expired, etc.)."""
    pass


class SessionExpiredError(AuthenticationError):
    """Broker API session has expired and needs re-authentication."""
    pass


class TOTPGenerationError(AuthenticationError):
    """Failed to generate TOTP token."""
    pass


# ── Rate Limiting & API ──────────────────────────────────────


class RateLimitError(TrendInvestorrError):
    """API rate limit exceeded. Contains retry_after_seconds if available."""

    def __init__(self, message: str = "Rate limit exceeded", retry_after: float = 1.0, **kwargs):
        self.retry_after = retry_after
        super().__init__(message, details={"retry_after_seconds": retry_after, **kwargs})


class APIError(TrendInvestorrError):
    """Generic broker API error with status code."""

    def __init__(self, message: str, status_code: int | None = None, **kwargs):
        self.status_code = status_code
        super().__init__(message, details={"status_code": status_code, **kwargs})


# ── Trading & Orders ─────────────────────────────────────────


class InsufficientFundsError(TrendInvestorrError):
    """Not enough margin/funds to place the order."""

    def __init__(self, required: float, available: float, **kwargs):
        self.required = required
        self.available = available
        super().__init__(
            f"Insufficient funds: required ₹{required:,.2f}, available ₹{available:,.2f}",
            details={"required": required, "available": available, **kwargs},
        )


class OrderRejectedError(TrendInvestorrError):
    """Broker rejected the order."""

    def __init__(self, symbol: str, reason: str, broker_order_id: str | None = None, **kwargs):
        self.symbol = symbol
        self.reason = reason
        self.broker_order_id = broker_order_id
        super().__init__(
            f"Order rejected for {symbol}: {reason}",
            details={"symbol": symbol, "reason": reason, "broker_order_id": broker_order_id, **kwargs},
        )


class PositionSquareOffError(TrendInvestorrError):
    """Failed to square off an intraday position before market close."""
    pass


# ── Risk Management ──────────────────────────────────────────


class CircuitBreakerOpenError(TrendInvestorrError):
    """Circuit breaker is OPEN — all trading is halted."""

    def __init__(self, reason: str, cooldown_minutes: int = 30, **kwargs):
        self.reason = reason
        self.cooldown_minutes = cooldown_minutes
        super().__init__(
            f"Circuit breaker OPEN: {reason}. Cooldown: {cooldown_minutes} min",
            details={"reason": reason, "cooldown_minutes": cooldown_minutes, **kwargs},
        )


class RiskLimitExceededError(TrendInvestorrError):
    """Trade would exceed configured risk limits (exposure, position size, etc.)."""
    pass


# ── Market & Timing ──────────────────────────────────────────


class MarketClosedError(TrendInvestorrError):
    """Attempted to trade outside market hours."""
    pass


class MarketHolidayError(MarketClosedError):
    """Today is an NSE holiday — no trading."""
    pass


# ── Data & Connectivity ─────────────────────────────────────


class DataFetchError(TrendInvestorrError):
    """Failed to fetch market data (bhavcopy, historical candles, etc.)."""
    pass


class WebSocketError(TrendInvestorrError):
    """WebSocket connection error (disconnect, timeout, etc.)."""
    pass


class InstrumentNotFoundError(TrendInvestorrError):
    """Stock symbol or token not found in the instrument master list."""

    def __init__(self, identifier: str, **kwargs):
        self.identifier = identifier
        super().__init__(
            f"Instrument not found: '{identifier}'",
            details={"identifier": identifier, **kwargs},
        )


# ── Security & Compliance ───────────────────────────────────


class VaultError(TrendInvestorrError):
    """Error with the encrypted credential vault (corrupt, wrong key, etc.)."""
    pass


class SEBIComplianceError(TrendInvestorrError):
    """Action would violate SEBI algo trading regulations."""
    pass


# ── Configuration ────────────────────────────────────────────


class ConfigurationError(TrendInvestorrError):
    """Invalid or missing configuration value."""
    pass


class DatabaseError(TrendInvestorrError):
    """Database connection or query error."""
    pass
