"""
Trend-Investorr: Test Data Factories

Builder-pattern factories for creating test instances of all 11 dataclasses.
Every test file imports from here — single source of truth for test data.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any

from core.constants import (
    AnalysisAction,
    CandleInterval,
    Exchange,
    IST,
    OrderStatus,
    OrderType,
    ProductType,
    SignalStatus,
    SignalStrength,
    SignalType,
    TransactionType,
)
from data.models.market_data import (
    AnalysisResult,
    Candle,
    DailyPnL,
    OrderRequest,
    OrderResult,
    Position,
    RiskDecision,
    Signal,
    Stock,
    Tick,
    VolumeSnapshot,
)


# ── Timestamp helpers ────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(IST)


def market_time(hour: int = 10, minute: int = 0) -> datetime:
    """Create a datetime during market hours today."""
    return datetime.now(IST).replace(hour=hour, minute=minute, second=0, microsecond=0)


# ── Stock Factory ────────────────────────────────────────────

def make_stock(
    symbol: str = "RELIANCE",
    token: str = "2885",
    name: str = "Reliance Industries Ltd",
    exchange: Exchange = Exchange.NSE,
    series: str = "EQ",
    sector: str = "OIL_GAS",
    industry: str = "Refineries & Marketing",
    market_cap_cr: float = 18000.0,
    lot_size: int = 1,
) -> Stock:
    return Stock(
        symbol=symbol, token=token, name=name, exchange=exchange,
        series=series, sector=sector, industry=industry,
        market_cap_cr=market_cap_cr, lot_size=lot_size,
    )


# ── Candle Factory ───────────────────────────────────────────

def make_candle(
    symbol: str = "RELIANCE",
    open: float = 2800.0,
    high: float = 2850.0,
    low: float = 2780.0,
    close: float = 2830.0,
    volume: int = 500_000,
    timestamp: datetime | None = None,
    interval: CandleInterval = CandleInterval.ONE_DAY,
    delivery_pct: float = 0.0,
) -> Candle:
    return Candle(
        symbol=symbol,
        timestamp=timestamp or now_ist(),
        open=open, high=high, low=low, close=close,
        volume=volume, interval=interval,
        delivery_pct=delivery_pct,
    )


def make_candle_series(
    symbol: str = "RELIANCE",
    count: int = 60,
    base_price: float = 2800.0,
    daily_change: float = 5.0,
    base_volume: int = 500_000,
) -> list[Candle]:
    """Create a realistic candle series with gradual price movement."""
    candles = []
    for i in range(count):
        price = base_price + (i * daily_change)
        ts = now_ist() - timedelta(days=count - i)
        candles.append(Candle(
            symbol=symbol,
            timestamp=ts,
            open=price,
            high=price + 20,
            low=price - 15,
            close=price + 10,
            volume=base_volume + (i * 1000),
            interval=CandleInterval.ONE_DAY,
        ))
    return candles


# ── Tick Factory ─────────────────────────────────────────────

def make_tick(
    symbol: str = "RELIANCE",
    token: str = "2885",
    ltp: float = 2830.0,
    volume: int = 500_000,
    timestamp: datetime | None = None,
    open: float = 2800.0,
    high: float = 2850.0,
    low: float = 2780.0,
    close: float = 2810.0,
    best_bid: float = 2829.0,
    best_ask: float = 2831.0,
) -> Tick:
    return Tick(
        symbol=symbol, token=token, ltp=ltp, volume=volume,
        timestamp=timestamp or now_ist(),
        open=open, high=high, low=low, close=close,
        best_bid=best_bid, best_ask=best_ask,
    )


# ── VolumeSnapshot Factory ──────────────────────────────────

def make_volume_snapshot(
    symbol: str = "RELIANCE",
    cumulative_volume: int = 1_000_000,
    avg_volume_30d: int = 500_000,
    avg_volume_90d: int = 480_000,
) -> VolumeSnapshot:
    return VolumeSnapshot(
        symbol=symbol,
        timestamp=now_ist(),
        cumulative_volume=cumulative_volume,
        avg_volume_30d=avg_volume_30d,
        avg_volume_90d=avg_volume_90d,
    )


# ── Signal Factory ───────────────────────────────────────────

def make_signal(
    symbol: str = "RELIANCE",
    token: str = "2885",
    signal_type: SignalType = SignalType.BUY,
    strength: SignalStrength = SignalStrength.STRONG,
    score: float = 0.65,
    price: float = 2830.0,
    sector: str = "OIL_GAS",
    volume_current: int = 1_000_000,
    volume_avg: int = 500_000,
    volume_ratio: float = 3.0,
    metadata: dict | None = None,
) -> Signal:
    return Signal(
        symbol=symbol, token=token, signal_type=signal_type,
        strength=strength, score=score, price=price,
        sector=sector, volume_current=volume_current,
        volume_avg=volume_avg, volume_ratio=volume_ratio,
        metadata=metadata or {},
    )


# ── AnalysisResult Factory ──────────────────────────────────

def make_analysis_result(
    action: AnalysisAction = AnalysisAction.BUY,
    confidence: float = 0.75,
    reasoning: str = "Strong uptrend with volume confirmation",
    analyzer_name: str = "static",
    signal_symbol: str = "RELIANCE",
    indicators: dict | None = None,
) -> AnalysisResult:
    return AnalysisResult(
        action=action, confidence=confidence, reasoning=reasoning,
        analyzer_name=analyzer_name, signal_symbol=signal_symbol,
        indicators=indicators or {"rsi": 55.0, "ema_short": 2810.0, "ema_long": 2750.0},
    )


# ── OrderRequest Factory ────────────────────────────────────

def make_order_request(
    symbol: str = "RELIANCE",
    token: str = "2885",
    transaction_type: TransactionType = TransactionType.BUY,
    quantity: int = 10,
    price: float = 2830.0,
    product_type: ProductType = ProductType.INTRADAY,
    trigger_price: float = 0.0,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, token=token, transaction_type=transaction_type,
        quantity=quantity, price=price, product_type=product_type,
        trigger_price=trigger_price,
    )


# ── Position Factory ────────────────────────────────────────

def make_position(
    symbol: str = "RELIANCE",
    token: str = "2885",
    quantity: int = 10,
    avg_buy_price: float = 2800.0,
    current_price: float = 2830.0,
    product_type: ProductType = ProductType.INTRADAY,
    is_paper: bool = True,
) -> Position:
    return Position(
        symbol=symbol, token=token, quantity=quantity,
        avg_buy_price=avg_buy_price, current_price=current_price,
        product_type=product_type, is_paper=is_paper,
    )
