"""
Trend-Investorr: Constants & Enums

All application-wide constants, enums, and static configuration values.
These are compile-time constants — for runtime-tunable values, use app_config DB table.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from datetime import time
from zoneinfo import ZoneInfo


# ── Timezone ─────────────────────────────────────────────────

IST = ZoneInfo("Asia/Kolkata")


# ── Market Hours ─────────────────────────────────────────────

MARKET_OPEN = time(9, 15)       # 9:15 AM IST
MARKET_CLOSE = time(15, 30)     # 3:30 PM IST
PRE_MARKET_OPEN = time(9, 0)    # 9:00 AM IST
PRE_MARKET_CLOSE = time(9, 15)  # 9:15 AM IST

# Scheduler time boundaries
PREMARKET_WARMUP_START = time(8, 45)
INTRADAY_SQUAREOFF_TIME = time(15, 15)  # Square off at 3:15 PM (15 min buffer)
POST_MARKET_START = time(15, 45)
POST_MARKET_END = time(17, 0)
NIGHT_IDLE_START = time(17, 0)
NIGHT_IDLE_END = time(8, 45)


# ── Exchange ─────────────────────────────────────────────────

class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE Futures & Options
    MCX = "MCX"  # Multi Commodity Exchange


# ── Order Types ──────────────────────────────────────────────

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOPLOSS_LIMIT"
    STOP_LOSS_MARKET = "STOPLOSS_MARKET"


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"   # MIS — squared off same day
    DELIVERY = "CNC"        # Cash and carry — hold overnight
    CARRYFORWARD = "NRML"   # Normal — F&O carry forward


class OrderVariety(str, Enum):
    NORMAL = "NORMAL"
    STOPLOSS = "STOPLOSS"
    AMO = "AMO"             # After Market Order
    ROBO = "ROBO"           # Bracket Order


class OrderStatus(str, Enum):
    CREATED = "CREATED"       # Internal — before sending to broker
    PLACED = "PLACED"         # Sent to broker, pending execution
    FILLED = "FILLED"         # Fully executed
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"     # Broker rejected
    CANCELLED = "CANCELLED"   # User/system cancelled


class Duration(str, Enum):
    DAY = "DAY"
    IOC = "IOC"  # Immediate or Cancel


# ── Signal Types ─────────────────────────────────────────────

class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStrength(str, Enum):
    WEAK = "WEAK"           # Volume 1.5x-2x average
    MODERATE = "MODERATE"   # Volume 2x-3x average
    STRONG = "STRONG"       # Volume 3x-5x average
    EXTREME = "EXTREME"     # Volume 5x+ average


class SignalStatus(str, Enum):
    PENDING = "PENDING"     # Generated, not yet acted upon
    ACTED = "ACTED"         # Order placed based on this signal
    EXPIRED = "EXPIRED"     # Cooldown passed without action
    SKIPPED = "SKIPPED"     # Analyzer rejected the signal


class SignalSource(str, Enum):
    """Tracks which scanner originated a signal for performance analysis."""
    VOLUME_SPIKE = "volume_spike"
    MOMENTUM = "momentum"
    BREAKOUT = "breakout"
    ORB = "orb"
    DELIVERY = "delivery"
    VWAP = "vwap"
    GAP = "gap"
    RELATIVE_STRENGTH = "relative_strength"
    SECTOR_ROTATION = "sector_rotation"
    ORDER_FLOW = "order_flow"
    PATTERN = "pattern"
    MTF_CONFLUENCE = "mtf_confluence"
    LIQUIDITY_SWEEP = "liquidity_sweep"


# ── Analyzer ─────────────────────────────────────────────────

class AnalyzerType(str, Enum):
    STATIC = "static"
    GEMINI = "gemini"
    COMPOSITE = "composite"


class CompositeStrategy(str, Enum):
    WEIGHTED = "weighted"       # Weighted average of confidence scores
    UNANIMOUS = "unanimous"     # ALL analyzers must agree
    MAJORITY = "majority"       # >50% must agree
    PRIMARY = "primary"         # First analyzer decides, others enrich


class AnalysisAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SKIP = "SKIP"
    HOLD = "HOLD"


# ── Circuit Breaker ──────────────────────────────────────────

class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"           # Normal operation
    OPEN = "OPEN"               # Trading halted
    HALF_OPEN = "HALF_OPEN"     # Testing with 1 trade


# ── WebSocket Feed Modes ─────────────────────────────────────

class FeedMode(IntEnum):
    LTP = 1         # Last Traded Price only
    QUOTE = 2       # Full quote snapshot
    SNAP_QUOTE = 3  # OHLCV + Volume snapshot


# ── Candle Intervals ─────────────────────────────────────────

class CandleInterval(str, Enum):
    ONE_MINUTE = "ONE_MINUTE"
    FIVE_MINUTE = "FIVE_MINUTE"
    FIFTEEN_MINUTE = "FIFTEEN_MINUTE"
    ONE_HOUR = "ONE_HOUR"
    ONE_DAY = "ONE_DAY"
    ONE_WEEK = "ONE_WEEK"


# Map CandleInterval → database table suffix
# Table names follow the pattern: candles_{suffix}
CANDLE_TABLE_SUFFIX: dict[str, str] = {
    CandleInterval.ONE_MINUTE: "1m",
    CandleInterval.FIVE_MINUTE: "5m",
    CandleInterval.FIFTEEN_MINUTE: "15m",
    CandleInterval.ONE_HOUR: "1h",
    CandleInterval.ONE_DAY: "1d",
    CandleInterval.ONE_WEEK: "1w",
}

# All candle table names for schema generation
CANDLE_TABLES = [f"candles_{suffix}" for suffix in CANDLE_TABLE_SUFFIX.values()]

# Backfill policy: how many days of history to fetch per interval
# Angel One API max is ~2 years (~730 days)
BACKFILL_DAYS: dict[str, int] = {
    CandleInterval.ONE_MINUTE: 7,         # 7 days (~2,625 candles/stock) — covers a full week for illiquid stocks
    CandleInterval.FIVE_MINUTE: 90,       # 90 days (~6,750 candles/stock)
    CandleInterval.FIFTEEN_MINUTE: 30,    # 30 days (~750 candles/stock)
    CandleInterval.ONE_HOUR: 90,          # 3 months (~540 candles/stock)
    CandleInterval.ONE_DAY: 7300,         # Full lifetime (~20 years, chunked into 2000-day API requests)
    CandleInterval.ONE_WEEK: 730,         # Lifetime (~104 candles/stock, aggregated from daily)
}


# ── Broker ───────────────────────────────────────────────────

class BrokerName(str, Enum):
    ANGEL_ONE = "angel_one"
    ZERODHA = "zerodha"
    FYERS = "fyers"
    UPSTOX = "upstox"


# ── App Config Categories ───────────────────────────────────

class ConfigCategory(str, Enum):
    TRADING = "trading"
    SCANNER = "scanner"
    ANALYZER = "analyzer"
    SIGNALS = "signals"
    RISK = "risk"
    SCHEDULER = "scheduler"


# ── Audit Event Types ───────────────────────────────────────

class AuditEventType(str, Enum):
    # Auth
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_RENEWED = "SESSION_RENEWED"

    # Trading
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    POSITION_SQUARED_OFF = "POSITION_SQUARED_OFF"

    # Signals
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    SIGNAL_VALIDATED = "SIGNAL_VALIDATED"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"

    # Risk
    CIRCUIT_BREAKER_OPENED = "CIRCUIT_BREAKER_OPENED"
    CIRCUIT_BREAKER_CLOSED = "CIRCUIT_BREAKER_CLOSED"
    CIRCUIT_BREAKER_CHANGE = "CIRCUIT_BREAKER_CHANGE"
    RISK_LIMIT_HIT = "RISK_LIMIT_HIT"

    # Config
    CONFIG_CHANGED = "CONFIG_CHANGED"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    VAULT_ACCESSED = "VAULT_ACCESSED"

    # System
    SYSTEM_START = "SYSTEM_START"
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"
    ERROR = "ERROR"
    SCHEDULER_TASK = "SCHEDULER_TASK"


# ── SmartAPI Constants ───────────────────────────────────────

SMARTAPI_MAX_WEBSOCKET_CONNECTIONS = 3
SMARTAPI_MAX_TOKENS_PER_CONNECTION = 1000
SMARTAPI_MAX_ORDERS_PER_SECOND = 10
SMARTAPI_SAFE_OPS_LIMIT = 8  # Safety margin below SEBI's 10 OPS
SMARTAPI_SESSION_EXPIRY_HOUR = 0  # Midnight daily logout

SMARTAPI_INSTRUMENT_LIST_URL = (
    "https://marginapi.angelbroking.com/OpenAPIService/rest/secure/"
    "angelbroking/instrument/v1/getMasterData"
)

# ── NSE Constants ────────────────────────────────────────────

NSE_BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{date}.csv"
)
NSE_REQUIRED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nseindia.com",
}


# ── NSE Trading Calendar (powered by exchange_calendars) ─────
# Provides accurate holiday data from 2003 onwards, covering all years.
# Lazily initialized singleton to avoid import-time overhead.

_nse_calendar = None


def _get_nse_calendar():
    """Lazily load the NSE (XNSE) exchange calendar singleton."""
    global _nse_calendar
    if _nse_calendar is None:
        import exchange_calendars as xcals
        _nse_calendar = xcals.get_calendar("XBOM")
    return _nse_calendar


def is_nse_trading_day(d) -> bool:
    """
    Check if a given date is an NSE trading session.

    Args:
        d: date object or ISO date string (e.g. "2026-03-26").

    Returns:
        True if NSE was open on that day, False if weekend/holiday.
    """
    from datetime import date as _date
    import pandas as pd

    if isinstance(d, str):
        d = _date.fromisoformat(d)

    cal = _get_nse_calendar()
    ts = pd.Timestamp(d)
    return cal.is_session(ts)


def get_nse_trading_days(start_date, end_date) -> list:
    """
    Get all valid NSE trading sessions between start_date and end_date (inclusive).

    Args:
        start_date: date object or ISO string.
        end_date:   date object or ISO string.

    Returns:
        Sorted list of date objects representing valid trading days.
    """
    from datetime import date as _date
    import pandas as pd

    if isinstance(start_date, str):
        start_date = _date.fromisoformat(start_date)
    if isinstance(end_date, str):
        end_date = _date.fromisoformat(end_date)

    cal = _get_nse_calendar()
    sessions = cal.sessions_in_range(
        pd.Timestamp(start_date),
        pd.Timestamp(end_date),
    )
    return [s.date() for s in sessions]

