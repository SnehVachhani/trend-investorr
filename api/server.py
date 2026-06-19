"""
Trend-Investorr: REST API Server

Lightweight FastAPI layer that exposes the existing PostgreSQL database
to the Next.js frontend. Reuses the same Database and Store classes
as the trading backend — zero code duplication.

Run:
    uvicorn api.server:app --reload --port 8000
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger

# ── Ensure project root is on sys.path ────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from storage.database import Database
from storage.models import (
    CandleStore,
    CandleCoverageStore,
    StockStore,
    TechnicalIndicatorStore,
    WatchlistStore,
    WatchlistAlgorithmBindingStore,
    ScannerSignalStore,
    AppConfigStore,
)
from config.settings import Settings

# ── App & Config ──────────────────────────────────────────────

app = FastAPI(
    title="Trend-Investorr API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database Connection (lazy singleton) ──────────────────────

_db: Database | None = None
_stores: dict = {}
_ingestion = None  # Lazy DataIngestionService for on-demand backfill


def get_db() -> Database:
    global _db, _stores
    if _db is None:
        settings = Settings()
        _db = Database(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            dbname=settings.DB_NAME,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            min_connections=1,
            max_connections=5,
        )
        _db.connect()
        _stores["candle"] = CandleStore(_db)
        _stores["coverage"] = CandleCoverageStore(_db)
        _stores["stock"] = StockStore(_db)
        _stores["indicator"] = TechnicalIndicatorStore(_db)
        _stores["watchlist"] = WatchlistStore(_db)
        _stores["binding"] = WatchlistAlgorithmBindingStore(_db)
        _stores["scanner_signal"] = ScannerSignalStore(_db)
        _stores["config"] = AppConfigStore(_db)
    return _db


def store(name: str):
    get_db()
    return _stores[name]


def get_ingestion():
    """Lazy-init DataIngestionService for on-demand backfill."""
    global _ingestion
    if _ingestion is not None:
        return _ingestion

    try:
        from data.data_ingestion import DataIngestionService
        from data.fetchers.historical_fetcher import HistoricalFetcher
        from data.auth.smartapi_auth import SmartAPISession
        from core.security.vault import SecureVault

        settings = Settings()
        vault = SecureVault(vault_path=settings.vault_dir)
        session = SmartAPISession(vault=vault)
        session.login()

        fetcher = HistoricalFetcher(session=session)
        db = get_db()
        _ingestion = DataIngestionService(
            historical_fetcher=fetcher,
            candle_store=store("candle"),
            stock_store=store("stock"),
            coverage_store=store("coverage"),
        )
        return _ingestion
    except Exception as e:
        logger.warning(f"Could not init DataIngestionService for on-demand backfill: {e}")
        return None


# ── Response Models ───────────────────────────────────────────

class StockResponse(BaseModel):
    id: int
    symbol: str
    token: str
    name: str
    exchange: str
    series: str
    sector_id: Optional[int] = None
    industry: str = ""
    market_cap_cr: float = 0
    is_active: bool = True


class CandleResponse(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float = 0


class IndicatorResponse(BaseModel):
    date: str
    rsi_14: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    atr_14: Optional[float] = None
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    adx_14: Optional[float] = None
    bollinger_upper: Optional[float] = None
    bollinger_lower: Optional[float] = None
    bollinger_width: Optional[float] = None
    supertrend: Optional[float] = None
    supertrend_direction: Optional[int] = None
    obv: Optional[int] = None
    vwap_daily: Optional[float] = None
    delivery_pct: Optional[float] = None
    pivot_point: Optional[float] = None
    pivot_r1: Optional[float] = None
    pivot_r2: Optional[float] = None
    pivot_s1: Optional[float] = None
    pivot_s2: Optional[float] = None
    support_level: Optional[float] = None
    resistance_level: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None


# ── Utility ───────────────────────────────────────────────────

INTERVAL_MAP = {
    "1m": "ONE_MINUTE",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
    "1w": "ONE_WEEK",
}

TABLE_MAP = {
    "1m": "candles_1m",
    "5m": "candles_5m",
    "15m": "candles_15m",
    "1h": "candles_1h",
    "1d": "candles_1d",
    "1w": "candles_1w",
}


def _serialize_row(row: dict) -> dict:
    """Convert datetime/Decimal fields to JSON-safe types."""
    result = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, date):
            result[k] = v.isoformat()
        elif hasattr(v, "__float__"):
            result[k] = float(v)
        else:
            result[k] = v
    return result


def _resolve_stock_id(db: Database, symbol: str) -> dict | None:
    """
    Find a stock by symbol, handling the '-EQ' suffix convention.

    Tries in order:
        1. Exact match (e.g. 'ADANIPORTS-EQ')
        2. With '-EQ' appended (e.g. 'ADANIPORTS' → 'ADANIPORTS-EQ')
        3. Case-insensitive ILIKE fallback
    """
    sym = symbol.upper()
    # Try exact match first
    stock = db.fetch_one(
        "SELECT * FROM stocks WHERE symbol = %s AND exchange = 'NSE' LIMIT 1",
        (sym,),
    )
    if stock:
        return stock

    # Try with -EQ suffix
    if not sym.endswith("-EQ"):
        stock = db.fetch_one(
            "SELECT * FROM stocks WHERE symbol = %s AND exchange = 'NSE' LIMIT 1",
            (f"{sym}-EQ",),
        )
        if stock:
            return stock

    # Fuzzy fallback
    stock = db.fetch_one(
        "SELECT * FROM stocks WHERE symbol ILIKE %s AND exchange = 'NSE' LIMIT 1",
        (f"{sym}%",),
    )
    return stock


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════


# ── Stocks ────────────────────────────────────────────────────

@app.get("/api/stocks")
def list_stocks(
    q: str = Query("", description="Search query (symbol or name)"),
    exchange: str = Query("NSE", description="Exchange filter"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Search and list stocks. Supports fuzzy search on symbol/name."""
    db = get_db()
    if q:
        rows = db.fetch_all(
            """
            SELECT s.id, s.symbol, s.token, s.name, s.exchange, s.series,
                   s.sector_id, s.industry, s.market_cap_cr, s.is_active,
                   lc.close AS latest_price
            FROM stocks s
            LEFT JOIN LATERAL (
                SELECT close FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT 1
            ) lc ON TRUE
            WHERE s.is_active = TRUE
              AND s.exchange = %s
              AND (s.symbol ILIKE %s OR s.name ILIKE %s)
            ORDER BY
                CASE WHEN s.symbol ILIKE %s THEN 0 ELSE 1 END,
                s.symbol
            LIMIT %s OFFSET %s
            """,
            (exchange, f"%{q}%", f"%{q}%", f"{q}%", limit, offset),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT s.id, s.symbol, s.token, s.name, s.exchange, s.series,
                   s.sector_id, s.industry, s.market_cap_cr, s.is_active,
                   lc.close AS latest_price
            FROM stocks s
            LEFT JOIN LATERAL (
                SELECT close FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT 1
            ) lc ON TRUE
            WHERE s.is_active = TRUE AND s.exchange = %s
            ORDER BY s.symbol
            LIMIT %s OFFSET %s
            """,
            (exchange, limit, offset),
        )
    return {"stocks": [_serialize_row(r) for r in rows], "count": len(rows)}


@app.get("/api/stocks/{symbol}")
def get_stock(symbol: str):
    """Get detailed stock info with latest indicators."""
    db = get_db()
    resolved = _resolve_stock_id(db, symbol)
    if not resolved:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    stock = db.fetch_one(
        """
        SELECT s.*, sec.name AS sector_name, sec.display_name AS sector_display
        FROM stocks s
        LEFT JOIN sectors sec ON sec.id = s.sector_id
        WHERE s.id = %s
        LIMIT 1
        """,
        (resolved["id"],),
    )
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    # Get latest indicators
    indicators = db.fetch_one(
        """
        SELECT * FROM technical_indicators
        WHERE stock_id = %s
        ORDER BY date DESC LIMIT 1
        """,
        (stock["id"],),
    )

    # Get latest daily candle for price info
    latest_candle = db.fetch_one(
        """
        SELECT open, high, low, close, volume, timestamp
        FROM candles_1d
        WHERE stock_id = %s
        ORDER BY timestamp DESC LIMIT 1
        """,
        (stock["id"],),
    )

    # Fallback: aggregate today's 1m candles into a synthetic daily bar
    if not latest_candle:
        latest_candle = db.fetch_one(
            """
            SELECT
                (ARRAY_AGG(open ORDER BY timestamp ASC))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (ARRAY_AGG(close ORDER BY timestamp DESC))[1] AS close,
                SUM(volume) AS volume,
                MAX(timestamp) AS timestamp
            FROM candles_1m
            WHERE stock_id = %s
              AND timestamp::DATE = (
                  SELECT MAX(timestamp::DATE) FROM candles_1m WHERE stock_id = %s
              )
            """,
            (stock["id"], stock["id"]),
        )

    # Get previous day candle for change calculation
    prev_candle = db.fetch_one(
        """
        SELECT close FROM candles_1d
        WHERE stock_id = %s
        ORDER BY timestamp DESC LIMIT 1 OFFSET 1
        """,
        (stock["id"],),
    )

    result = _serialize_row(stock)
    result["indicators"] = _serialize_row(indicators) if indicators else None
    result["latest_candle"] = _serialize_row(latest_candle) if latest_candle else None
    result["prev_close"] = float(prev_candle["close"]) if prev_candle else None
    return result


# ── Candles (Chart Data) ──────────────────────────────────────

@app.get("/api/stocks/{symbol}/candles")
def get_candles(
    symbol: str,
    interval: str = Query("1d", description="Candle interval: 1m, 5m, 15m, 1h, 1d, 1w"),
    days: int = Query(90, ge=1, le=730, description="Days of history"),
):
    """Get OHLCV candles for charting."""
    db = get_db()

    if interval not in TABLE_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval '{interval}'. Use: {', '.join(TABLE_MAP.keys())}",
        )

    stock = _resolve_stock_id(db, symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    table = TABLE_MAP[interval]
    rows = db.fetch_all(
        f"""
        SELECT timestamp, open, high, low, close, volume, vwap
        FROM {table}
        WHERE stock_id = %s
          AND timestamp >= NOW() - INTERVAL '{days} days'
        ORDER BY timestamp ASC
        """,
        (stock["id"],),
    )

    candles = []
    for r in rows:
        candles.append({
            "time": int(r["timestamp"].timestamp()),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["volume"]),
        })

    return {"symbol": symbol.upper(), "interval": interval, "candles": candles}


@app.post("/api/stocks/{symbol}/backfill")
def backfill_stock_candles(
    symbol: str,
    days: int = Query(365, ge=1, le=730, description="Days of history to fetch"),
):
    """
    On-demand backfill: fetch historical candle data for a stock.

    Use this when a newly added stock has no chart data, or to
    refresh stale data. Fetches 5m, 15m, 1h, and 1d candles from
    the broker API and stores them in the database.

    This is a synchronous operation that may take 10-30 seconds
    depending on the number of intervals and API rate limits.
    """
    db = get_db()
    stock = _resolve_stock_id(db, symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    ingestion = get_ingestion()
    if not ingestion:
        raise HTTPException(
            status_code=503,
            detail="Backfill service unavailable — broker session not connected",
        )

    try:
        stats = ingestion.backfill_stock(
            stock={"id": stock["id"], "symbol": stock["symbol"], "token": stock["token"]},
            days=days,
        )
        return {
            "symbol": stock["symbol"],
            "status": "completed",
            **stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backfill failed: {e}")


# ── Indicators ────────────────────────────────────────────────

@app.get("/api/stocks/{symbol}/indicators")
def get_indicators(
    symbol: str,
    days: int = Query(30, ge=1, le=365),
):
    """Get technical indicators history."""
    db = get_db()
    stock = _resolve_stock_id(db, symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    rows = db.fetch_all(
        """
        SELECT * FROM technical_indicators
        WHERE stock_id = %s AND date >= CURRENT_DATE - %s
        ORDER BY date DESC
        """,
        (stock["id"], days),
    )
    return {"symbol": symbol.upper(), "indicators": [_serialize_row(r) for r in rows]}


# ── Watchlists ────────────────────────────────────────────────

@app.get("/api/watchlists")
def list_watchlists():
    """Get all watchlists with stock counts."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT wl.id, wl.name, wl.description, wl.list_type, wl.is_active, wl.max_stocks,
               COUNT(wi.id) AS stock_count
        FROM watchlists wl
        LEFT JOIN watchlist_items wi ON wi.watchlist_id = wl.id
        GROUP BY wl.id
        ORDER BY wl.name
        """
    )
    return {"watchlists": [_serialize_row(r) for r in rows]}


@app.get("/api/watchlists/{watchlist_id}/stocks")
def get_watchlist_stocks(watchlist_id: int):
    """Get stocks in a specific watchlist with latest price."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT s.id, s.symbol, s.token, s.name, s.exchange, s.series,
               s.industry, s.market_cap_cr, wi.priority, wi.source, wi.added_at,
               latest.close AS last_price,
               latest.timestamp AS price_date,
               ath.ath_price
        FROM watchlist_items wi
        JOIN stocks s ON s.id = wi.stock_id
        LEFT JOIN LATERAL (
            SELECT close, timestamp
            FROM candles_1d
            WHERE stock_id = s.id
            ORDER BY timestamp DESC
            LIMIT 1
        ) latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(high) AS ath_price
            FROM candles_1d
            WHERE stock_id = s.id
        ) ath ON TRUE
        WHERE wi.watchlist_id = %s AND s.is_active = TRUE
        ORDER BY wi.priority DESC, s.symbol
        """,
        (watchlist_id,),
    )
    return {"stocks": [_serialize_row(r) for r in rows]}


@app.get("/api/watchlists/{watchlist_id}/prices")
def get_watchlist_prices(watchlist_id: int):
    """
    Get live prices for all stocks in a watchlist.

    Returns LTP + previous close so the frontend can compute change.
    Designed for polling every ~3s during market hours.

    Logic:
        - During market hours: LTP from live_ticks, prev_close from yesterday's candle.
        - After hours / weekends: LTP = latest candle close, prev_close = second-latest candle close.
    """
    db = get_db()
    rows = db.fetch_all(
        """
        WITH wl_stocks AS (
            SELECT s.id AS stock_id, s.token
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wi.watchlist_id = %s AND s.is_active = TRUE
        ),
        latest_tick AS (
            SELECT DISTINCT ON (lt.stock_id)
                   lt.stock_id,
                   lt.ltp,
                   lt.open AS day_open,
                   lt.high AS day_high,
                   lt.low AS day_low,
                   lt.volume,
                   lt.timestamp
            FROM live_ticks lt
            JOIN wl_stocks ws ON ws.stock_id = lt.stock_id
            ORDER BY lt.stock_id, lt.timestamp DESC
        ),
        recent_candles AS (
            SELECT stock_id, close, timestamp,
                   ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY timestamp DESC) AS rn
            FROM candles_1d
            WHERE stock_id IN (SELECT stock_id FROM wl_stocks)
        ),
        latest_candle AS (
            SELECT stock_id, close AS latest_close
            FROM recent_candles WHERE rn = 1
        ),
        prev_candle AS (
            SELECT stock_id, close AS prev_close
            FROM recent_candles WHERE rn = 2
        )
        SELECT ws.stock_id,
               t.ltp,
               t.day_open,
               t.day_high,
               t.day_low,
               t.volume,
               t.timestamp AS tick_time,
               lc.latest_close,
               pc.prev_close
        FROM wl_stocks ws
        LEFT JOIN latest_tick t ON t.stock_id = ws.stock_id
        LEFT JOIN latest_candle lc ON lc.stock_id = ws.stock_id
        LEFT JOIN prev_candle pc ON pc.stock_id = ws.stock_id
        """,
        (watchlist_id,),
    )

    prices = {}
    for r in rows:
        tick_ltp = float(r["ltp"]) if r["ltp"] else None
        latest_close = float(r["latest_close"]) if r["latest_close"] else None
        prev_close = float(r["prev_close"]) if r["prev_close"] else None

        if tick_ltp:
            # Live tick available — use it as LTP, prev_close = latest candle close
            ltp = tick_ltp
            ref_close = latest_close
        else:
            # No live tick (market closed) — LTP = latest candle, ref = previous candle
            ltp = latest_close
            ref_close = prev_close

        change = (ltp - ref_close) if (ltp and ref_close) else None
        change_pct = (change / ref_close * 100) if (change is not None and ref_close) else None

        prices[r["stock_id"]] = {
            "ltp": ltp,
            "prev_close": ref_close,
            "change": round(change, 2) if change is not None else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "day_open": float(r["day_open"]) if r["day_open"] else None,
            "day_high": float(r["day_high"]) if r["day_high"] else None,
            "day_low": float(r["day_low"]) if r["day_low"] else None,
            "volume": int(r["volume"]) if r["volume"] else None,
        }

    return {"prices": prices}


@app.get("/api/watchlists/{watchlist_id}/changes")
def get_watchlist_changes(watchlist_id: int):
    """
    Get multi-period price change percentages for all stocks in a watchlist.

    Periods: 1D, 5D, 10D, 20D, 30D, 60D, 90D.
    Uses calendar-date-based lookups to find the closest candle to N trading
    days ago.  This avoids the ROW_NUMBER() approach which gives wildly wrong
    results when there are gaps in the candle data.

    Each "N trading days" is converted to approximate calendar days
    (N * 7/5, rounded up) with a ±3-day tolerance window.  Within that
    window the candle closest to the target date is selected.
    """
    db = get_db()

    # ── 1. Fetch latest close per stock in the watchlist ─────────
    rows = db.fetch_all(
        """
        WITH wl_stocks AS (
            SELECT wi.stock_id, s.symbol, s.name
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wi.watchlist_id = %s AND s.is_active = TRUE
        )
        SELECT ws.stock_id, ws.symbol, ws.name,
               lc.close  AS close_now,
               lc.timestamp AS latest_ts
        FROM wl_stocks ws
        LEFT JOIN LATERAL (
            SELECT close, timestamp
            FROM candles_1d
            WHERE stock_id = ws.stock_id
            ORDER BY timestamp DESC
            LIMIT 1
        ) lc ON TRUE
        """,
        (watchlist_id,),
    )

    if not rows:
        return {"changes": []}

    # ── 2. For each period, fetch the reference close ────────────
    #    Trading-day → calendar-day approximation:
    #      1 td  →  1 cd     (just previous day)
    #      5 td  →  7 cd
    #     10 td  → 14 cd
    #     20 td  → 28 cd
    #     30 td  → 42 cd
    #     60 td  → 84 cd
    #     90 td  → 126 cd
    #    180 td  → 252 cd
    #      1 yr  → 365 cd
    #      3 yr  → 1095 cd
    PERIODS = [
        ("1d",   1),
        ("5d",   7),
        ("10d", 14),
        ("20d", 28),
        ("30d", 42),
        ("60d", 84),
        ("90d", 126),
        ("180d", 252),
        ("1y",  365),
        ("3y",  1095),
    ]

    stock_ids = [r["stock_id"] for r in rows]

    # Build a map:  stock_id -> { "1d": close, "5d": close, ... }
    ref_closes: dict[int, dict[str, float | None]] = {sid: {} for sid in stock_ids}

    for period_label, cal_days in PERIODS:
        # Find the candle closest to `cal_days` ago for each stock.
        # Tolerance window scales with period length to handle
        # extended holidays in historical data.
        # Exclude the latest candle itself so that 1D doesn't compare
        # a candle against itself.
        tolerance = 3 if cal_days <= 126 else (5 if cal_days <= 365 else 7)
        period_rows = db.fetch_all(
            """
            SELECT DISTINCT ON (c.stock_id)
                   c.stock_id, c.close
            FROM candles_1d c
            WHERE c.stock_id = ANY(%s)
              AND c.timestamp < (
                  SELECT MAX(c2.timestamp)
                  FROM candles_1d c2
                  WHERE c2.stock_id = c.stock_id
              )
              AND c.timestamp::date BETWEEN
                    (CURRENT_DATE - %s - %s)
                    AND
                    (CURRENT_DATE - %s + %s)
            ORDER BY c.stock_id,
                     ABS(c.timestamp::date - (CURRENT_DATE - %s)) ASC
            """,
            (stock_ids, cal_days, tolerance, cal_days, tolerance, cal_days),
        )
        for pr in period_rows:
            ref_closes[pr["stock_id"]][period_label] = float(pr["close"])

    # ── 3. Compute percentage changes ───────────────────────────
    changes = []
    for r in rows:
        now = float(r["close_now"]) if r["close_now"] else None
        entry: dict = {
            "stock_id": r["stock_id"],
            "symbol": r["symbol"],
            "name": r["name"],
            "close": now,
        }
        for period_label, _ in PERIODS:
            old = ref_closes[r["stock_id"]].get(period_label)
            if now and old and old != 0:
                entry[period_label] = round((now - old) / old * 100, 2)
            else:
                entry[period_label] = None
        changes.append(entry)

    return {"changes": changes}


class ReorderRequest(BaseModel):
    stock_ids: list[int]  # ordered list — first = highest priority


@app.put("/api/watchlists/{watchlist_id}/reorder")
def reorder_watchlist_stocks(watchlist_id: int, req: ReorderRequest):
    """Reorder stocks in a watchlist by updating priority values."""
    db = get_db()
    wl = db.fetch_one(
        "SELECT id FROM watchlists WHERE id = %s AND is_active = TRUE",
        (watchlist_id,),
    )
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    total = len(req.stock_ids)
    for idx, stock_id in enumerate(req.stock_ids):
        db.execute(
            "UPDATE watchlist_items SET priority = %s WHERE watchlist_id = %s AND stock_id = %s",
            (total - idx, watchlist_id, stock_id),
        )

    return {"reordered": True, "count": total}


class CreateWatchlistRequest(BaseModel):
    name: str
    description: str = ""
    list_type: str = "custom"
    max_stocks: int = 500


@app.post("/api/watchlists")
def create_watchlist(req: CreateWatchlistRequest):
    """Create a new watchlist."""
    ws: WatchlistStore = store("watchlist")
    wl_id = ws.create_watchlist(
        name=req.name,
        description=req.description,
        list_type=req.list_type,
        max_stocks=req.max_stocks,
    )
    if wl_id is None:
        raise HTTPException(status_code=500, detail="Failed to create watchlist")
    return {"id": wl_id, "name": req.name, "status": "created"}


@app.delete("/api/watchlists/{watchlist_id}")
def delete_watchlist(watchlist_id: int):
    """Delete (soft-delete) a watchlist by ID."""
    db = get_db()
    # Get watchlist name for the store method
    wl = db.fetch_one("SELECT name FROM watchlists WHERE id = %s", (watchlist_id,))
    if not wl:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")

    ws: WatchlistStore = store("watchlist")
    deleted = ws.delete_watchlist(wl["name"])
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")
    return {"deleted": True, "id": watchlist_id}


class AddStockRequest(BaseModel):
    stock_id: int
    source: str = "manual"


@app.post("/api/watchlists/{watchlist_id}/stocks")
def add_stock_to_watchlist(watchlist_id: int, req: AddStockRequest):
    """Add a stock to a watchlist. Auto-backfills candle data if missing."""
    db = get_db()
    wl = db.fetch_one("SELECT name, max_stocks FROM watchlists WHERE id = %s AND is_active = TRUE", (watchlist_id,))
    if not wl:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")

    # Verify stock exists
    stock = db.fetch_one("SELECT id, symbol, token FROM stocks WHERE id = %s", (req.stock_id,))
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {req.stock_id} not found")

    ws: WatchlistStore = store("watchlist")
    added = ws.add_stock(wl["name"], req.stock_id, req.source)
    if not added:
        raise HTTPException(
            status_code=409,
            detail="Stock already in watchlist or watchlist is full",
        )

    # Auto-backfill candle data if this stock has no daily candles yet
    has_candles = db.fetch_one(
        "SELECT 1 FROM candles_1d WHERE stock_id = %s LIMIT 1",
        (stock["id"],),
    )
    backfill_status = "skipped"
    if not has_candles:
        import threading
        def _bg_backfill():
            try:
                ingestion = get_ingestion()
                if ingestion:
                    ingestion.backfill_stock(stock)
                    logger.info(f"Auto-backfill completed for {stock['symbol']}")
            except Exception as e:
                logger.error(f"Auto-backfill failed for {stock['symbol']}: {e}")
        threading.Thread(target=_bg_backfill, daemon=True, name=f"Backfill-{stock['symbol']}").start()
        backfill_status = "started"

    return {
        "added": True,
        "watchlist_id": watchlist_id,
        "stock_id": req.stock_id,
        "symbol": stock["symbol"],
        "backfill": backfill_status,
    }


@app.delete("/api/watchlists/{watchlist_id}/stocks/{stock_id}")
def remove_stock_from_watchlist(watchlist_id: int, stock_id: int):
    """Remove a stock from a watchlist."""
    db = get_db()
    wl = db.fetch_one("SELECT name FROM watchlists WHERE id = %s", (watchlist_id,))
    if not wl:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")

    ws: WatchlistStore = store("watchlist")
    ws.remove_stock(wl["name"], stock_id)
    return {"removed": True, "watchlist_id": watchlist_id, "stock_id": stock_id}


class PopulateRequest(BaseModel):
    method: str = "volume"   # "volume" or "all_active"
    limit: int = 500


@app.post("/api/watchlists/{watchlist_id}/populate")
def populate_watchlist(watchlist_id: int, req: PopulateRequest):
    """
    Auto-populate a watchlist with stocks.

    Methods:
        volume     — Top N stocks by 30-day avg volume (default)
        all_active — All active NSE stocks
    """
    db = get_db()
    wl = db.fetch_one(
        "SELECT id, name FROM watchlists WHERE id = %s AND is_active = TRUE",
        (watchlist_id,),
    )
    if not wl:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")

    ws: WatchlistStore = store("watchlist")

    if req.method == "volume":
        count = ws.populate_from_volume(wl["name"], limit=req.limit)
    elif req.method == "all_active":
        # Insert all active stocks from the stocks table
        rows = db.fetch_all(
            """
            SELECT id FROM stocks
            WHERE is_active = TRUE AND exchange = 'NSE'
            ORDER BY market_cap_cr DESC
            LIMIT %s
            """,
            (req.limit,),
        )
        count = 0
        for row in rows:
            added = ws.add_stock(wl["name"], row["id"], source="auto_populate")
            if added:
                count += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown method '{req.method}'. Use 'volume' or 'all_active'.")

    return {
        "watchlist_id": watchlist_id,
        "watchlist_name": wl["name"],
        "method": req.method,
        "stocks_added": count,
    }


# ── Signals ───────────────────────────────────────────────────

@app.get("/api/signals")
def list_signals(
    date_filter: Optional[str] = Query(None, alias="date", description="Filter by date (YYYY-MM-DD or 'today')"),
    signal_type: Optional[str] = Query(None, description="BUY or SELL"),
    limit: int = Query(50, ge=1, le=200),
):
    """Get trading signals."""
    db = get_db()
    conditions = []
    params = []

    if date_filter == "today":
        conditions.append("sig.created_at::DATE = CURRENT_DATE")
    elif date_filter:
        conditions.append("sig.created_at::DATE = %s")
        params.append(date_filter)

    if signal_type:
        conditions.append("sig.signal_type = %s")
        params.append(signal_type.upper())

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = db.fetch_all(
        f"""
        SELECT sig.*, s.symbol, s.name
        FROM signals sig
        JOIN stocks s ON s.id = sig.stock_id
        {where}
        ORDER BY sig.created_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return {"signals": [_serialize_row(r) for r in rows]}


@app.get("/api/scanner-signals")
def list_scanner_signals(
    date_filter: Optional[str] = Query(None, alias="date", description="Filter by target_date"),
    limit: int = Query(50, ge=1, le=200),
):
    """Get scanner signals (overnight batch results)."""
    db = get_db()
    params = []
    conditions = []

    if date_filter == "today":
        conditions.append("ss.target_date = CURRENT_DATE")
    elif date_filter:
        conditions.append("ss.target_date = %s")
        params.append(date_filter)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    rows = db.fetch_all(
        f"""
        SELECT ss.*, s.symbol, s.name
        FROM scanner_signals ss
        JOIN stocks s ON s.id = ss.stock_id
        {where}
        ORDER BY ss.confidence DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return {"scanner_signals": [_serialize_row(r) for r in rows]}


# ── Orders ────────────────────────────────────────────────────

@app.get("/api/orders")
def list_orders(
    date_filter: Optional[str] = Query(None, alias="date"),
    status: Optional[str] = Query(None),
    is_paper: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
):
    """Get order history."""
    db = get_db()
    conditions = ["o.is_paper_trade = %s"]
    params: list = [is_paper]

    if date_filter == "today":
        conditions.append("o.created_at::DATE = CURRENT_DATE")
    elif date_filter:
        conditions.append("o.created_at::DATE = %s")
        params.append(date_filter)

    if status:
        conditions.append("o.status = %s")
        params.append(status.upper())

    where = f"WHERE {' AND '.join(conditions)}"
    params.append(limit)

    rows = db.fetch_all(
        f"""
        SELECT o.*, s.symbol, s.name
        FROM orders o
        JOIN stocks s ON s.id = o.stock_id
        {where}
        ORDER BY o.created_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return {"orders": [_serialize_row(r) for r in rows]}


# ── Positions ─────────────────────────────────────────────────

@app.get("/api/positions")
def list_positions(is_paper: bool = Query(True)):
    """Get current open positions."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT p.*, s.symbol, s.name, s.token
        FROM positions p
        JOIN stocks s ON s.id = p.stock_id
        WHERE p.is_paper = %s AND p.quantity > 0
        ORDER BY p.unrealized_pnl DESC
        """,
        (is_paper,),
    )
    return {"positions": [_serialize_row(r) for r in rows]}


# ── Portfolio & P&L ───────────────────────────────────────────

@app.get("/api/portfolio/pnl")
def get_pnl(
    days: int = Query(30, ge=1, le=365),
    is_paper: bool = Query(True),
):
    """Get daily P&L history."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT * FROM daily_pnl
        WHERE is_paper = %s
        ORDER BY date DESC
        LIMIT %s
        """,
        (is_paper, days),
    )
    return {"pnl": [_serialize_row(r) for r in rows]}


@app.get("/api/portfolio/summary")
def get_portfolio_summary(is_paper: bool = Query(True)):
    """Get portfolio summary — capital, deployed, P&L."""
    db = get_db()

    # Total P&L
    pnl_row = db.fetch_one(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS total_pnl FROM daily_pnl WHERE is_paper = %s",
        (is_paper,),
    )

    # Today's P&L
    today_pnl = db.fetch_one(
        """
        SELECT COALESCE(realized_pnl, 0) AS realized, COALESCE(unrealized_pnl, 0) AS unrealized,
               COALESCE(total_trades, 0) AS trades, COALESCE(winning_trades, 0) AS wins,
               COALESCE(losing_trades, 0) AS losses, COALESCE(portfolio_value, 0) AS portfolio_value
        FROM daily_pnl
        WHERE date = CURRENT_DATE AND is_paper = %s
        """,
        (is_paper,),
    )

    # Open positions count and value
    pos_row = db.fetch_one(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(quantity * current_price), 0) AS total_value,
               COALESCE(SUM(unrealized_pnl), 0) AS unrealized_pnl
        FROM positions
        WHERE is_paper = %s AND quantity > 0
        """,
        (is_paper,),
    )

    return {
        "total_capital": 10_000_000,  # From RiskConfig default
        "total_pnl": float(pnl_row["total_pnl"]) if pnl_row else 0,
        "today": _serialize_row(today_pnl) if today_pnl else None,
        "positions": _serialize_row(pos_row) if pos_row else {"count": 0, "total_value": 0, "unrealized_pnl": 0},
    }


# ── System ────────────────────────────────────────────────────

@app.get("/api/system/status")
def get_system_status():
    """Get system health and status."""
    db = get_db()
    healthy = db.health_check()

    # Stock count
    stock_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM stocks WHERE is_active = TRUE")

    # Coverage summary
    coverage = db.fetch_all(
        """
        SELECT interval, COUNT(*) AS stocks, SUM(candle_count) AS candles
        FROM candle_coverage
        GROUP BY interval ORDER BY interval
        """
    )

    return {
        "status": "running" if healthy else "degraded",
        "database": "connected" if healthy else "disconnected",
        "stocks_active": stock_count["cnt"] if stock_count else 0,
        "coverage": [_serialize_row(c) for c in coverage],
    }


@app.get("/api/system/config")
def get_system_config():
    """Get all app config values."""
    db = get_db()
    rows = db.fetch_all(
        "SELECT key, value, data_type, category, description FROM app_config ORDER BY category, key"
    )
    return {"config": [_serialize_row(r) for r in rows]}


@app.get("/api/coverage")
def get_coverage_summary():
    """Get candle data coverage summary."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT interval,
               COUNT(*) AS stock_count,
               MIN(earliest_ts) AS global_earliest,
               MAX(latest_ts) AS global_latest,
               SUM(candle_count) AS total_candles
        FROM candle_coverage
        GROUP BY interval
        ORDER BY interval
        """
    )
    return {"coverage": [_serialize_row(r) for r in rows]}


# ══════════════════════════════════════════════════════════════
# BACKTEST ENDPOINTS
# ══════════════════════════════════════════════════════════════


class BacktestRequest(BaseModel):
    algorithm: str
    symbol: str
    start_date: str = ""    # YYYY-MM-DD, empty = use all available data
    end_date: str = ""      # YYYY-MM-DD, empty = today
    initial_capital: float = 1_000_000


@app.get("/api/algorithms")
def list_algorithms():
    """Get all registered trading algorithms."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT name, display_name, description, strategy_type,
               candle_intervals, max_positions, stop_loss_pct, is_active
        FROM algorithms
        WHERE is_active = TRUE
        ORDER BY name
        """
    )
    return {"algorithms": [_serialize_row(r) for r in rows]}


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    """Run a backtest and persist the results."""
    import json as _json
    from strategies.backtester import Backtester

    db = get_db()

    # Resolve stock
    stock = _resolve_stock_id(db, req.symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {req.symbol} not found")

    # Build date filters
    date_conditions = ["stock_id = %s"]
    params: list = [stock["id"]]
    if req.start_date:
        date_conditions.append("timestamp >= %s")
        params.append(req.start_date)
    if req.end_date:
        date_conditions.append("timestamp <= %s")
        params.append(req.end_date)

    where = " AND ".join(date_conditions)

    # Fetch candles
    rows = db.fetch_all(
        f"""
        SELECT timestamp, open, high, low, close, volume
        FROM candles_1d
        WHERE {where}
        ORDER BY timestamp ASC
        """,
        tuple(params),
    )

    if len(rows) < 201:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient data: {len(rows)} candles found (need 201+ for SMA200 warmup)",
        )

    # Convert rows to dicts with float values
    candles = []
    for r in rows:
        candles.append({
            "timestamp": r["timestamp"],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["volume"]),
        })

    # Create run record
    run = db.execute_returning(
        """
        INSERT INTO backtest_runs
            (algorithm_name, stock_symbol, stock_name, start_date, end_date,
             initial_capital, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'running')
        RETURNING id
        """,
        (
            req.algorithm,
            stock["symbol"],
            stock.get("name", ""),
            candles[0]["timestamp"],
            candles[-1]["timestamp"],
            req.initial_capital,
        ),
    )
    run_id = run["id"]

    # Run backtest
    try:
        bt = Backtester(
            candles=candles,
            algorithm=req.algorithm,
            symbol=stock["symbol"],
            initial_capital=req.initial_capital,
        )
        result = bt.run()
    except Exception as e:
        db.execute(
            "UPDATE backtest_runs SET status = 'failed', error_message = %s WHERE id = %s",
            (str(e), run_id),
        )
        raise HTTPException(status_code=500, detail=f"Backtest failed: {e}")

    if result.error:
        db.execute(
            "UPDATE backtest_runs SET status = 'failed', error_message = %s WHERE id = %s",
            (result.error, run_id),
        )
        raise HTTPException(status_code=400, detail=result.error)

    # Downsample equity curve for storage (keep max ~500 points)
    eq = result.equity_curve
    if len(eq) > 500:
        step = len(eq) // 500
        eq = eq[::step] + [eq[-1]]

    # Update run with results
    db.execute(
        """
        UPDATE backtest_runs SET
            status = 'completed',
            total_return_pct = %s, cagr_pct = %s, max_drawdown_pct = %s,
            sharpe_ratio = %s, win_rate_pct = %s, total_trades = %s,
            winning_trades = %s, losing_trades = %s,
            avg_win_pct = %s, avg_loss_pct = %s, profit_factor = %s,
            avg_hold_days = %s, final_capital = %s,
            equity_curve = %s,
            start_date = %s, end_date = %s
        WHERE id = %s
        """,
        (
            result.total_return_pct, result.cagr_pct, result.max_drawdown_pct,
            result.sharpe_ratio, result.win_rate_pct, result.total_trades,
            result.winning_trades, result.losing_trades,
            result.avg_win_pct, result.avg_loss_pct, result.profit_factor,
            result.avg_hold_days, result.final_capital,
            _json.dumps(eq),
            str(result.start_date), str(result.end_date),
            run_id,
        ),
    )

    # Insert trades
    for t in result.trades:
        db.execute(
            """
            INSERT INTO backtest_trades
                (run_id, symbol, entry_date, exit_date, entry_price, exit_price,
                 quantity, pnl, pnl_pct, exit_reason, hold_days)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, t.symbol, str(t.entry_date),
                str(t.exit_date) if t.exit_date else None,
                t.entry_price, t.exit_price, t.quantity,
                t.pnl, t.pnl_pct, t.exit_reason, t.hold_days,
            ),
        )

    return {"run_id": run_id, "status": "completed"}


@app.get("/api/backtest/history")
def get_backtest_history(limit: int = Query(50, ge=1, le=200)):
    """List past backtest runs (like chat history sidebar)."""
    db = get_db()
    rows = db.fetch_all(
        """
        SELECT id, algorithm_name, stock_symbol, stock_name,
               start_date, end_date, initial_capital, status,
               total_return_pct, win_rate_pct, total_trades,
               max_drawdown_pct, sharpe_ratio, created_at
        FROM backtest_runs
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return {"runs": [_serialize_row(r) for r in rows]}


@app.get("/api/backtest/{run_id}")
def get_backtest_result(run_id: int):
    """Get full backtest result including trades and equity curve."""
    db = get_db()
    run = db.fetch_one(
        "SELECT * FROM backtest_runs WHERE id = %s",
        (run_id,),
    )
    if not run:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")

    trades = db.fetch_all(
        """
        SELECT * FROM backtest_trades
        WHERE run_id = %s
        ORDER BY entry_date ASC
        """,
        (run_id,),
    )

    result = _serialize_row(run)
    result["trades"] = [_serialize_row(t) for t in trades]
    return result


@app.delete("/api/backtest/{run_id}")
def delete_backtest(run_id: int):
    """Delete a backtest run and its trades."""
    db = get_db()
    affected = db.execute("DELETE FROM backtest_runs WHERE id = %s", (run_id,))
    if affected == 0:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")
    return {"deleted": True, "run_id": run_id}

