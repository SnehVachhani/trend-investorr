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
import json
import sys
import asyncio
import calendar
import hashlib
import math
import re
import socket
import threading
import uuid
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from multiprocessing import get_context
from threading import Event, Lock, Thread
from typing import Any, Callable, Literal, Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from loguru import logger

# ── Ensure project root is on sys.path ────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.makedirs(os.path.join(PROJECT_ROOT, "logs"), exist_ok=True)
logger.add(
    os.path.join(PROJECT_ROOT, "logs", "screener-query.log"),
    level="WARNING",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
    filter=lambda record: record["extra"].get("screener_query_refresh") is True,
    rotation="5 MB",
    retention="30 days",
)

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
    FilterProfileStore,
    TrendlineStore,
    FundamentalStore,
)
from config.settings import Settings
from data.ohlcv import is_valid_ohlcv
from core.exceptions import DatabaseError
from core.constants import BACKFILL_DAYS, CANDLE_TABLE_SUFFIX, CandleInterval, IST, MARKET_CLOSE
from core.universe import equity_universe_sql, is_equity_stock
from data.candle_sessions import (
    INTRADAY_TIMEFRAMES,
    as_ist_datetime,
    is_regular_session_candle,
    normalize_daily_candles,
)
from engine.filter_engine.catalog import build_default_catalog
from engine.filter_engine.context import build_evaluation_context
from engine.filter_engine.evaluator import FilterEvaluator
from engine.filter_engine.backtest_cache import build_atomic_backtest_cache
from engine.filter_engine.presets import INTRADAY_BARS_PER_TRADING_DAY, build_filter_presets, get_filter_preset
from engine.filter_engine.models import (
    EvaluationMode,
    FilterCondition,
    FilterExpression,
    FilterGroup,
    CandlePoint,
    Timeframe,
)
from strategies.profile_backtester import ProfileBacktestConfig, ProfileBacktester
from trading.charges import DeliveryChargeConfig, config_from_store
from engine.enhanced_momentum_scanner import build_market_context, enrich_candidates
from engine.fundamental_query import evaluate_universe, render_query, validate_rules
from strategies.fundamental_setup_backtester import (
    FILTER_BACKED_SETUP_IDS,
    FUNDAMENTAL_SETUP_CANDLE_INTERVAL,
    SETUPS,
    prepare_setup_candles,
    run_setup,
)
from scheduler.market_scheduler import MarketScheduler

# ── Startup Candle Synchronization ────────────────────────────

_candle_sync_lock = Lock()
_instrument_sync_lock = Lock()
_candle_sync_state = {
    "status": "idle",
    "started_at": None,
    "completed_at": None,
    "stats": None,
    "error": None,
}

# Watchlist analysis is intentionally asynchronous.  A full comparison has
# one CPU-heavy, point-in-time backtest batch per stock, so holding a browser
# request open for several minutes is both fragile and gives the UI no useful
# progress information.
_watchlist_analysis_lock = Lock()
_watchlist_analysis_jobs: dict[str, dict[str, Any]] = {}
# The process pool belongs to its in-memory job.  Keeping the handle lets a
# newer request stop CPU work that has become irrelevant rather than making
# the user wait for it to drain.
_watchlist_analysis_executors: dict[str, ProcessPoolExecutor] = {}
_MAX_WATCHLIST_ANALYSIS_JOBS = 20
# A filter batch is CPU-bound Python work.  Threads cannot execute those
# batches on separate cores, whereas independent stock batches can safely do
# so because every worker creates its own database connection.  Keep the pool
# bounded to leave CPU and database capacity for the API and live services.
# Use at most half the logical CPUs (capped at eight) to improve large
# watchlist throughput while retaining capacity for the API and data services.
_MAX_WATCHLIST_ANALYSIS_WORKERS = min(8, max(1, (os.cpu_count() or 1) // 2))

# Enhanced Momentum replay runs can scan every trading session and then evaluate
# multiple intraday filter overlays. Keep their progress in a small in-memory
# job registry so the UI can poll without holding one HTTP request open.
_enhanced_momentum_backtest_lock = Lock()
_enhanced_momentum_backtest_jobs: dict[str, dict[str, Any]] = {}
_MAX_ENHANCED_MOMENTUM_BACKTEST_JOBS = 20
# The registry above is only a cache for the jobs *this* process is running.
# Every job is also persisted (with its request) before its worker starts, so
# a restart cannot orphan a handle the UI is still polling. The owning worker
# heartbeats while it runs; another API process treats a job as alive while
# that heartbeat is fresh and only settles it once the heartbeat goes stale.
_ENHANCED_BACKTEST_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_ENHANCED_BACKTEST_HEARTBEAT_INTERVAL = timedelta(seconds=10)
_ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT = timedelta(seconds=60)
_ENHANCED_BACKTEST_ACTIVE_STATUSES = frozenset({"queued", "running"})
_ENHANCED_BACKTEST_INTERRUPTED_ERROR = (
    "The replay was interrupted: the API process running it stopped before the result was saved."
)
_ENHANCED_BACKTEST_SINGLE_ACTIVE_INDEX = "idx_enhanced_momentum_backtest_jobs_single_active"
_ENHANCED_BACKTEST_SUBMISSION_INDEX = "idx_enhanced_momentum_backtest_jobs_submission"
_ENHANCED_BACKTEST_BUSY_DETAIL = "A historical replay is already running. Wait for it to finish before starting another."


class _EnhancedBacktestOwnershipLost(RuntimeError):
    """This worker no longer owns the job: it was settled or replaced elsewhere.

    Raised by every storage write a worker makes (progress, heartbeat, status,
    result) once the fence rejects it, so the worker goes silent instead of
    overwriting the settled outcome or saving a late result.
    """
# Historical scans and per-stock setup batches are CPU-heavy and independent.
# Use up to 80% of logical CPUs (capped at 12) while keeping a portion available
# for the API process, the market-data services, and the rest of the desktop.
_MAX_ENHANCED_MOMENTUM_BACKTEST_WORKERS = min(
    12,
    max(1, math.ceil((os.cpu_count() or 1) * 0.8)),
)

# A refresh is handed to the user's regular Chrome profile by the local
# Screener connector extension. Keep one active job at a time.
_screener_query_lock = Lock()
_screener_query_jobs: dict[str, dict[str, Any]] = {}
_MAX_SCREENER_QUERY_JOBS = 10
_SCREENER_QUERY_JOB_TIMEOUT = timedelta(minutes=15)

# All daily feature scans use one row per stock and exchange session.  A
# uniquely close-stamped row is the preferred EOD representation; conflicting
# duplicates without that marker are omitted because this schema has no source
# or corporate-action provenance with which to choose safely.
_CANONICAL_DAILY_SESSION_CTES = """
daily_source AS (
    SELECT
        c.stock_id,
        c.timestamp,
        c.open,
        c.high,
        c.low,
        c.close,
        c.volume,
        c.delivery_pct,
        (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date AS session_date,
        (c.timestamp AT TIME ZONE 'Asia/Kolkata')::time AS session_time,
        ROW_NUMBER() OVER (
            PARTITION BY c.stock_id, (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY
                ((c.timestamp AT TIME ZONE 'Asia/Kolkata')::time = TIME '15:30') DESC,
                c.timestamp DESC
        ) AS session_row_rank,
        COUNT(*) OVER (
            PARTITION BY c.stock_id, (c.timestamp AT TIME ZONE 'Asia/Kolkata')::date
        ) AS session_row_count
    FROM candles_1d c
    JOIN universe u ON u.id = c.stock_id
    -- Callers bind IST-midnight timestamptz boundaries. Keep the indexed
    -- column bare here; convert to IST only when deriving the session date.
    WHERE c.timestamp >= %s
      AND c.timestamp < %s
), daily_variants AS (
    SELECT
        stock_id,
        session_date,
        COUNT(DISTINCT ROW(open, high, low, close, volume, delivery_pct)) AS variant_count
    FROM daily_source
    GROUP BY stock_id, session_date
), daily_sessions AS (
    SELECT
        source.stock_id,
        ((source.session_date + TIME '15:30') AT TIME ZONE 'Asia/Kolkata') AS timestamp,
        source.session_date,
        source.open,
        source.high,
        source.low,
        source.close,
        source.volume,
        source.delivery_pct
    FROM daily_source source
    JOIN daily_variants variants USING (stock_id, session_date)
    WHERE source.session_row_rank = 1
      AND (
          source.session_row_count = 1
          OR variants.variant_count = 1
          OR source.session_time = TIME '15:30'
      )
)
"""


def _last_completed_daily_scan_date(requested_as_of: date) -> date:
    """Avoid treating an in-progress current-session daily bar as complete."""
    now = datetime.now(IST)
    if requested_as_of >= now.date() and now.time().replace(tzinfo=None) < MARKET_CLOSE:
        return now.date() - timedelta(days=1)
    return requested_as_of


def _enhanced_scan_history_days(min_history: int) -> int:
    """Convert the largest required daily-session window to a calendar range."""
    required_sessions = max(min_history, 253)  # 252-session return plus its base close
    return math.ceil(required_sessions * 365.25 / 252 * 1.10)

def _terminate_watchlist_analysis_executor(executor: ProcessPoolExecutor) -> None:
    """Immediately stop queued and active child work for a superseded job."""
    # Python 3.12's executor has no public terminate_workers() API.  Each
    # backtest persists in separate database operations, and PostgreSQL rolls
    # back an operation interrupted during process termination, so stopping
    # the child processes cannot leave a partially written backtest row.
    processes = getattr(executor, "_processes", None) or {}
    for process in list(processes.values()):
        if process.is_alive():
            process.terminate()
    executor.shutdown(wait=False, cancel_futures=True)


def _candle_sync_snapshot() -> dict:
    """Return a thread-safe snapshot of the latest candle synchronization."""
    with _candle_sync_lock:
        return dict(_candle_sync_state)


def _candle_sync_completion_summary(stats: Any) -> tuple[str, int, int]:
    """Distinguish a clean reconciliation from one that left failures or gaps."""
    errors = 0
    unresolved = 0

    def visit(value: Any) -> None:
        nonlocal errors, unresolved
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    if key == "errors" or key.endswith("_errors"):
                        errors += max(0, int(item))
                    elif key.endswith("_unresolved"):
                        unresolved += max(0, int(item))
                    else:
                        visit(item)
                elif key != "by_exchange":
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(stats)
    if errors:
        return "completed_with_errors", errors, unresolved
    if unresolved:
        return "completed_with_gaps", errors, unresolved
    return "completed", errors, unresolved


def _run_candle_sync(cycle_name: str, *, full_reconcile: bool = False) -> None:
    """Reconcile candle history while publishing progress to the status API."""
    with _candle_sync_lock:
        if _candle_sync_state["status"] == "running":
            return
        _candle_sync_state.update(
            {
                "status": "running",
                "started_at": datetime.now(IST).isoformat(),
                "completed_at": None,
                "stats": None,
                "error": None,
            }
        )

    logger.info("{} candle synchronization started", cycle_name)
    try:
        ingestion = get_ingestion()
        if ingestion is None:
            raise RuntimeError("data ingestion service is unavailable")

        if full_reconcile:
            # Startup reconciles coverage metadata and repairs stale boundaries.
            stats = ingestion.backfill_all_intervals(exchange="ALL")
        else:
            stats = ingestion.sync_pending_all_intervals(exchange="ALL")
    except Exception as exc:
        with _candle_sync_lock:
            _candle_sync_state.update(
                {
                    "status": "failed",
                    "completed_at": datetime.now(IST).isoformat(),
                    "error": str(exc),
                }
            )
        logger.error("{} candle synchronization failed: {}", cycle_name, exc)
        return

    with _candle_sync_lock:
        completion_status, error_count, unresolved_count = (
            _candle_sync_completion_summary(stats)
        )
        _candle_sync_state.update(
            {
                "status": completion_status,
                "completed_at": datetime.now(IST).isoformat(),
                "stats": stats,
            }
        )
    logger.info(
        "{} candle synchronization finished with status {} ({} errors, {} unresolved gaps)",
        cycle_name,
        completion_status,
        error_count,
        unresolved_count,
    )


def _run_startup_candle_sync() -> None:
    """Run the initial full history sync when candle coverage is absent."""
    _run_candle_sync("Startup", full_reconcile=True)


def _sync_instrument_master(*, force_refresh: bool = False) -> None:
    """Refresh NSE/BSE instruments and persist equities before candle sync."""
    with _instrument_sync_lock:
        try:
            from data.fetchers.instrument_loader import InstrumentLoader

            settings = Settings()
            instruments = InstrumentLoader(
                cache_dir=settings.data_dir / "instruments",
            )
            instrument_count = instruments.load(force_refresh=force_refresh)
            stock_count = instruments.sync_to_db(get_db())
            logger.info(
                "Instrument master refresh complete: {} instruments read, "
                "{} NSE/BSE equities and benchmarks upserted",
                instrument_count,
                stock_count,
            )
        except Exception as exc:
            # Keep the API available if the broker's public instrument-master
            # feed is temporarily unavailable. The loader falls back to cache.
            logger.exception("Instrument master refresh failed: {}", exc)


def _has_existing_five_minute_coverage() -> bool:
    """Avoid repeat full syncs while detecting newly added BSE stock rows."""
    try:
        row = get_db().fetch_one(
            f"""
            SELECT
                EXISTS (
                    SELECT 1 FROM candle_coverage
                    WHERE interval = %s
                ) AS present,
                EXISTS (
                    SELECT 1
                    FROM stocks s
                    WHERE s.exchange = 'BSE'
                      AND {equity_universe_sql('s')}
                      AND NOT EXISTS (
                          SELECT 1 FROM candle_coverage cc
                          WHERE cc.stock_id = s.id AND cc.interval = %s
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM candle_sync_checks sc
                          WHERE sc.stock_id = s.id
                            AND sc.interval = %s
                            AND sc.last_result = 'empty'
                      )
                ) AS bse_missing
            """,
            (
                CandleInterval.FIVE_MINUTE.value,
                CandleInterval.FIVE_MINUTE.value,
                CandleInterval.FIVE_MINUTE.value,
            ),
        )
        return bool(row and row.get("present") and not row.get("bse_missing"))
    except Exception as exc:
        logger.warning(
            "Could not inspect candle coverage before startup sync ({})",
            type(exc).__name__,
        )
        return False


def _run_scheduled_candle_sync() -> None:
    """Run the same coverage-aware repair after each market close."""
    _run_candle_sync("Post-market")


def _start_startup_candle_sync() -> None:
    """Reconcile candles after the stock universe has been refreshed."""
    if _has_existing_five_minute_coverage():
        logger.info(
            "Existing five-minute history found; starting pending-candle reconciliation"
        )
        _run_candle_sync("Startup")
    else:
        _run_startup_candle_sync()


def _run_initial_instrument_and_candle_sync() -> None:
    """Refresh stocks before starting the first candle-coverage pass."""
    _sync_instrument_master()
    _start_startup_candle_sync()


_candle_scheduler: MarketScheduler | None = None


def _start_candle_scheduler() -> None:
    """Schedule a coverage-aware candle sync after each NSE/BSE trading day."""
    global _candle_scheduler
    if _candle_scheduler is not None:
        return

    scheduler = MarketScheduler()
    scheduler.register_pre_market(
        lambda: _sync_instrument_master(force_refresh=True)
    )
    scheduler.register_candle_sync(_run_scheduled_candle_sync)
    scheduler.start()
    _candle_scheduler = scheduler


def _stop_candle_scheduler() -> None:
    global _candle_scheduler
    if _candle_scheduler is not None:
        _candle_scheduler.stop(wait=False)
        _candle_scheduler = None


_fundamentals_scheduler: MarketScheduler | None = None
_fundamentals_service = None
_fundamentals_store: FundamentalStore | None = None
_fundamentals_cycle_lock = Lock()
_fundamentals_stop_event = Event()


def _run_fundamentals_sync_cycle(endpoints: tuple[str, ...], cycle_name: str) -> None:
    """Run one serialized endpoint group in rate-limited batches."""
    settings = Settings()
    service = _fundamentals_service
    fundamental_store = _fundamentals_store
    if service is None or fundamental_store is None:
        return
    with _fundamentals_cycle_lock:
        try:
            with fundamental_store.sync_cycle_lock(wait=False) as acquired:
                if not acquired:
                    logger.info("Upstox fundamentals cycle is already running in another process")
                    return

                max_batches = max(1, settings.UPSTOX_MAX_BATCHES_PER_CYCLE)
                interval_seconds = max(1, settings.UPSTOX_BATCH_INTERVAL_MINUTES) * 60
                cooldown = fundamental_store.rate_limit_cooldown_seconds(interval_seconds)
                if cooldown > 0:
                    logger.info(
                        "Upstox {} cycle waiting {:.1f} seconds for the previous batch cooldown",
                        cycle_name,
                        cooldown,
                    )
                    if _fundamentals_stop_event.wait(cooldown):
                        return

                for batch_number in range(1, max_batches + 1):
                    if _fundamentals_stop_event.is_set():
                        break
                    stats = service.sync_due(batch_number=batch_number, endpoints=endpoints)
                    if stats.already_running:
                        logger.info("Upstox fundamentals sync skipped: another process owns the active run")
                        return
                    attempted = stats.successful_requests + stats.failed_requests
                    logger.info(
                        "Upstox {} batch {}/{}: mapped_listings={} unique_isins={} success={} failed={} pending={}",
                        cycle_name,
                        batch_number,
                        max_batches,
                        stats.mapped_stocks,
                        stats.unique_isins,
                        stats.successful_requests,
                        stats.failed_requests,
                        stats.pending_requests,
                    )
                    if stats.pending_requests <= 0 or attempted <= 0:
                        break
                    if batch_number < max_batches and _fundamentals_stop_event.wait(interval_seconds):
                        break
        except Exception as exc:
            logger.exception(f"Upstox fundamentals {cycle_name} cycle failed: {exc}")


def _start_fundamentals_scheduler() -> None:
    """Initialize the read-only Upstox sync for API-only deployments."""
    global _fundamentals_scheduler, _fundamentals_service, _fundamentals_store
    if _fundamentals_scheduler is not None:
        return
    _fundamentals_stop_event.clear()

    settings = Settings()
    try:
        from core.security.vault import SecureVault
        from data.fundamentals.instrument_resolver import UpstoxInstrumentResolver
        from data.fundamentals.service import FundamentalsSyncService
        from data.fundamentals.upstox_client import UpstoxFundamentalsClient

        daily_endpoints = FundamentalsSyncService.DAILY_ENDPOINTS
        weekly_endpoints = FundamentalsSyncService.WEEKLY_ENDPOINTS + FundamentalsSyncService.MONTHLY_ENDPOINTS

        vault = SecureVault(settings.vault_dir, settings.VAULT_MASTER_KEY)
        if settings.UPSTOX_ANALYTICS_TOKEN:
            vault.store_credential("UPSTOX_ANALYTICS_TOKEN", settings.UPSTOX_ANALYTICS_TOKEN)
        token = vault.get_credential("UPSTOX_ANALYTICS_TOKEN") or settings.UPSTOX_ANALYTICS_TOKEN
        if not token:
            logger.info("Upstox fundamentals sync is disabled: Analytics Token is not configured")
            return

        get_db()
        _fundamentals_service = FundamentalsSyncService(
            client=UpstoxFundamentalsClient(
                token=token,
                base_url=settings.UPSTOX_API_BASE_URL,
                timeout_seconds=settings.UPSTOX_HTTP_TIMEOUT_SECONDS,
                max_retries=settings.UPSTOX_MAX_RETRIES,
                requests_per_minute=settings.UPSTOX_REQUESTS_PER_MINUTE,
                requests_per_30_minutes=settings.UPSTOX_REQUESTS_PER_30_MINUTES,
            ),
            resolver=UpstoxInstrumentResolver(
                cache_dir=settings.data_dir / "upstox_instruments",
                url=settings.UPSTOX_INSTRUMENTS_URL,
            ),
            fundamental_store=store("fundamental"),
            stock_store=store("stock"),
            max_requests_per_run=settings.UPSTOX_MAX_REQUESTS_PER_RUN,
            max_stocks_per_run=settings.UPSTOX_MAX_STOCKS_PER_RUN,
            profile_refresh_days=settings.UPSTOX_PROFILE_REFRESH_DAYS,
            statement_refresh_days=settings.UPSTOX_STATEMENT_REFRESH_DAYS,
            ratio_refresh_days=settings.UPSTOX_RATIO_REFRESH_DAYS,
            shareholding_refresh_days=settings.UPSTOX_SHAREHOLDING_REFRESH_DAYS,
            corporate_action_refresh_days=settings.UPSTOX_CORPORATE_ACTION_REFRESH_DAYS,
            competitor_refresh_days=settings.UPSTOX_COMPETITOR_REFRESH_DAYS,
        )
        _fundamentals_store = store("fundamental")

        scheduler = MarketScheduler()
        scheduler.register_fundamentals_sync(
            lambda: _run_fundamentals_sync_cycle(daily_endpoints, "daily"),
            hour=settings.UPSTOX_SYNC_HOUR,
            minute=settings.UPSTOX_SYNC_MINUTE,
            name="fundamentals_daily_sync",
            day_of_week="*",
            description="Upstox daily ratios and corporate actions",
        )
        scheduler.register_fundamentals_sync(
            lambda: _run_fundamentals_sync_cycle(weekly_endpoints, "weekly"),
            hour=settings.UPSTOX_WEEKLY_SYNC_HOUR,
            minute=settings.UPSTOX_WEEKLY_SYNC_MINUTE,
            name="fundamentals_weekly_sync",
            day_of_week="sat",
            description="Upstox Saturday statements and monthly company data",
        )
        scheduler.start()
        _fundamentals_scheduler = scheduler
        logger.info("Upstox fundamentals scheduler started in the API process")

        now = datetime.now(IST)
        daily_scheduled_time = now.replace(
            hour=settings.UPSTOX_SYNC_HOUR,
            minute=settings.UPSTOX_SYNC_MINUTE,
            second=0,
            microsecond=0,
        )
        weekly_scheduled_time = now.replace(
            hour=settings.UPSTOX_WEEKLY_SYNC_HOUR,
            minute=settings.UPSTOX_WEEKLY_SYNC_MINUTE,
            second=0,
            microsecond=0,
        )
        weekly_catchup = now.weekday() == 5 and now - weekly_scheduled_time > timedelta(minutes=5)
        daily_catchup = now - daily_scheduled_time > timedelta(minutes=5)
        if weekly_catchup or daily_catchup:
            def catch_up_missed_fundamentals_cycles() -> None:
                if weekly_catchup:
                    _run_fundamentals_sync_cycle(weekly_endpoints, "weekly catch-up")
                if daily_catchup:
                    _run_fundamentals_sync_cycle(daily_endpoints, "daily catch-up")

            Thread(
                target=catch_up_missed_fundamentals_cycles,
                name="upstox-fundamentals-catch-up",
                daemon=True,
            ).start()
    except Exception as exc:
        _fundamentals_service = None
        logger.exception(f"Upstox fundamentals scheduler could not start: {exc}")


def _stop_fundamentals_scheduler() -> None:
    global _fundamentals_scheduler, _fundamentals_service, _fundamentals_store
    _fundamentals_stop_event.set()
    if _fundamentals_scheduler is not None:
        _fundamentals_scheduler.stop(wait=False)
        _fundamentals_scheduler = None
        _fundamentals_service = None
        _fundamentals_store = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the API and its background data synchronization services."""
    # The fundamentals scheduler may persist its Upstox token in the shared
    # encrypted vault. Complete that write before candle sync reads the same
    # vault for SmartAPI credentials.
    _start_fundamentals_scheduler()
    _start_candle_scheduler()
    # Keep API availability independent from the public instrument-master
    # request. The worker upserts stocks first, then starts candle sync.
    Thread(
        target=_run_initial_instrument_and_candle_sync,
        name="startup-instrument-and-candle-sync",
        daemon=True,
    ).start()
    try:
        yield
    finally:
        _stop_candle_scheduler()
        _stop_fundamentals_scheduler()


# ── App & Config ──────────────────────────────────────────────

app = FastAPI(
    title="Trend-Investorr API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database Connection (lazy singleton) ──────────────────────

_db: Database | None = None
_stores: dict = {}
_ingestion = None  # Lazy DataIngestionService for on-demand backfill
_db_init_lock = Lock()
_is_cpu_analysis_worker = False


def _initialize_cpu_analysis_worker() -> None:
    """Mark a spawned CPU worker so it opens, but does not migrate, its DB."""
    global _is_cpu_analysis_worker
    _is_cpu_analysis_worker = True


def get_db() -> Database:
    global _db, _stores
    if _db is None:
        with _db_init_lock:
            if _db is None:
                settings = Settings()
                db = Database(
                    host=settings.DB_HOST,
                    port=settings.DB_PORT,
                    dbname=settings.DB_NAME,
                    user=settings.DB_USER,
                    password=settings.DB_PASSWORD,
                    min_connections=settings.DB_MIN_CONNECTIONS,
                    max_connections=settings.DB_MAX_CONNECTIONS,
                )
                db.connect()
                # API deployments may run without the standalone trading
                # process. Keep additive schema objects available without
                # touching Docker-managed database resources.
                # The API process performs the idempotent migrations before a
                # job is submitted.  Spawned CPU workers only need a fresh
                # connection; racing migrations in every child adds startup
                # delay and unnecessary database locks.
                skip_startup_migrations = os.getenv("TREND_INVESTOR_SKIP_MIGRATIONS", "").strip().lower() in {
                    "1", "true", "yes", "on",
                }
                if not _is_cpu_analysis_worker and not skip_startup_migrations:
                    db.run_migrations()
                _stores["candle"] = CandleStore(db)
                _stores["coverage"] = CandleCoverageStore(db)
                _stores["stock"] = StockStore(db)
                _stores["indicator"] = TechnicalIndicatorStore(db)
                _stores["watchlist"] = WatchlistStore(db)
                _stores["binding"] = WatchlistAlgorithmBindingStore(db)
                _stores["scanner_signal"] = ScannerSignalStore(db)
                _stores["config"] = AppConfigStore(db)
                _stores["filter_profile"] = FilterProfileStore(db)
                _stores["trendline"] = TrendlineStore(db)
                _stores["fundamental"] = FundamentalStore(db)
                # Existing databases can have provider profile snapshots while
                # the dedicated stock market-cap column is still at its
                # schema default. Backfill it once when the API opens. This
                # uses the stored market-cap field directly and never derives
                # capitalisation from P/E.
                db.execute(
                    """
                    UPDATE stocks AS s
                    SET market_cap_cr = profiles.market_cap_cr,
                        updated_at = NOW()
                    FROM (
                        SELECT DISTINCT ON (stock_id)
                               stock_id,
                               sector_market_cap_inr AS market_cap_cr
                        FROM company_fundamental_profiles
                        WHERE source = 'upstox'
                          AND sector_market_cap_inr IS NOT NULL
                          AND sector_market_cap_inr > 0
                        ORDER BY stock_id, snapshot_date DESC, fetched_at DESC
                    ) AS profiles
                    WHERE s.id = profiles.stock_id
                      AND COALESCE(s.market_cap_cr, 0) <= 0
                    """
                )
                _db = db
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
        from core.security.audit import AuditTrail

        settings = Settings()
        vault = SecureVault(vault_path=settings.vault_dir, master_key=getattr(settings, "VAULT_MASTER_KEY", ""))
        audit = AuditTrail(log_dir=settings.audit_dir)
        session = SmartAPISession(vault=vault, audit=audit)
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


class FilterRequirementsRequest(BaseModel):
    mode: EvaluationMode = EvaluationMode.SCREEN
    expression: dict[str, Any]


class FilterProfileCreateRequest(BaseModel):
    name: str
    description: str = ""
    mode: EvaluationMode = EvaluationMode.SCREEN
    owner_id: str | None = None
    expression: dict[str, Any]
    risk_config: dict[str, Any] = {}
    timeframe_config: dict[str, Any] = {}


class FilterProfileDraftRequest(BaseModel):
    mode: EvaluationMode = EvaluationMode.SCREEN
    expression: dict[str, Any]
    risk_config: dict[str, Any] = {}
    timeframe_config: dict[str, Any] = {}
    expected_version_id: int | None = None


class FilterProfilePublishRequest(BaseModel):
    version_id: int
    mode: EvaluationMode = EvaluationMode.SIGNAL


class FilterProfileBindingRequest(BaseModel):
    version_id: int
    target_type: str
    target_id: str
    direction: str = "BUY"
    enabled: bool = True


class TrendlineCreateRequest(BaseModel):
    symbol: str
    interval: str = "1d"
    points: list[dict[str, float | int]]
    trendline_type: str = "support"
    source: str = "manual"
    name: Optional[str] = None


class TrendlineUpdateRequest(BaseModel):
    points: list[dict[str, float | int]]
    active: bool = True


class FilterPreviewRequest(BaseModel):
    symbols: list[str] = []
    mode: EvaluationMode = EvaluationMode.SCREEN
    as_of: datetime | None = None
    limit: int = 100


class FilterBacktestRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float = 100_000
    position_size_pct: float = 100
    # Legacy request field retained for old clients. Charge configuration is
    # now sourced from the Trading costs master module.
    commission_pct: float | None = None
    slippage_pct: float = 0
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_hold_bars: int | None = None
    # Entry filters are not automatically treated as trailing exit rules.
    # Callers can opt in when they explicitly want signal invalidation exits.
    close_on_filter_fail: bool = False


class FilterBacktestBatchRequest(FilterBacktestRequest):
    """Shared backtest inputs plus the curated combinations to run."""

    # An omitted or empty list means every currently available preset.
    preset_ids: list[str] | None = None


class WatchlistFilterAnalysisRequest(BaseModel):
    """Inputs for a multi-stock comparison of the curated filter presets."""

    # A saved watchlist and pasted symbols may be combined; duplicate symbols
    # are removed before work begins.
    watchlist_id: int | None = None
    symbols: list[str] = Field(default_factory=list)
    start_date: str
    end_date: str
    # This is one equal-weight portfolio fund for the selected watchlist.  It
    # is divided across stocks before their independent simulations begin.
    initial_capital: float = 1_000_000
    preset_ids: list[str] | None = None


class MomentumScanRequest(BaseModel):
    """Controls for the daily, universe-wide momentum ranking."""

    as_of_date: date | None = None
    max_results: int = Field(default=100, ge=1, le=500)
    min_price: float = Field(default=20, ge=0)
    min_avg_turnover_cr: float = Field(default=1, ge=0)
    min_score: float = Field(default=55, ge=0, le=100)
    min_volume_ratio: float = Field(default=1.2, ge=0)
    min_return_20d: float = -100
    min_return_60d: float = -100
    min_delivery_pct: float = Field(default=0, ge=0, le=100)
    trend_mode: Literal["aligned", "above_sma50", "any"] = "aligned"


class EnhancedMomentumScanRequest(BaseModel):
    """Controls for the additive, market-aware daily swing scanner."""

    as_of_date: date | None = None
    # No hidden result cap: leave this empty to keep every stock that passes
    # the scanner rules, or explicitly select the highest-ranked N stocks.
    top_n: int | None = Field(default=None, ge=1, le=5000)
    min_price: float = Field(default=20, ge=0)
    min_avg_turnover_cr: float = Field(default=1, ge=0)
    min_score: float = Field(default=55, ge=0, le=100)
    min_volume_ratio: float = Field(default=1.2, ge=0)
    min_return_20d: float = -100
    min_return_60d: float = -100
    min_history: int = Field(default=260, ge=60, le=1000)
    trend_mode: Literal["aligned", "above_sma50", "any"] = "aligned"
    market_mode: Literal["all", "bullish_or_neutral", "bullish_only"] = "all"
    require_breakout: bool = False


class EnhancedAthMarkersRequest(BaseModel):
    """Load ATH markers for an already-rendered set of scan results."""

    stock_ids: list[int] = Field(min_length=1, max_length=5000)
    as_of_date: date


class EnhancedSetupCandlesRequest(BaseModel):
    """Load a bounded daily-candle window for setup scans over candidates."""

    stock_ids: list[int] = Field(min_length=1, max_length=5000)
    as_of_date: date
    days: int = Field(default=120, ge=15, le=730)


class MasterCandidateMetricsRequest(BaseModel):
    """Load enhanced daily metrics for stocks supplied by a Master source."""

    stock_ids: list[int] = Field(min_length=1, max_length=5000)
    as_of_date: date


class ScreenerQueryRefreshRequest(BaseModel):
    """Queue one Screener query for the user's regular Chrome profile."""

    query: str = Field(min_length=1, max_length=5000)


class ScreenerQueryRefreshProgressRequest(BaseModel):
    """Progress reported by the installed local Chrome connector."""

    status: Literal["running", "waiting_for_login"] = "running"
    phase: str = Field(default="Reading Screener results", max_length=120)
    pages_completed: int = Field(default=0, ge=0, le=250)
    candidates_found: int = Field(default=0, ge=0, le=5000)
    detail: str = Field(default="", max_length=500)


class ScreenerQueryRefreshCompleteRequest(BaseModel):
    """Collected Screener rows returned from the regular Chrome profile."""

    page_count: int = Field(ge=0, le=250)
    rows: list[dict[str, Any]] = Field(max_length=5000)


class ScreenerQueryRefreshFailRequest(BaseModel):
    error: str = Field(min_length=1, max_length=500)


class EnhancedMomentumBacktestRequest(BaseModel):
    """Historical replay window and point-in-time enhanced scanner filters."""

    # Caller-supplied, stable identity for one submission. Retrying with the
    # same identity returns the original job and its outcome instead of
    # starting another replay; reusing it with different parameters is
    # rejected. Omit it to always request a fresh run.
    submission_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    start_date: date | None = None
    end_date: date | None = None
    lookback_months: int = Field(default=3, ge=1, le=24)
    mode: Literal["filter_replay", "buy_and_hold"] = "filter_replay"
    top_n: Literal[30, 50, 100] = 50
    refresh_frequency: Literal["15_days", "monthly", "quarterly", "semiannual"] = "monthly"
    target_atr: float = Field(default=2.0, gt=0, le=10)
    stop_atr: float = Field(default=1.0, gt=0, le=10)
    capital_per_stock: float = Field(default=100_000, ge=1_000, le=10_000_000)
    preset_ids: list[str] = Field(default_factory=list, max_length=20)
    fundamental_setup_ids: list[str] = Field(default_factory=list, max_length=20)
    include_top_n_buy_hold: bool = False
    filters: EnhancedMomentumScanRequest = Field(default_factory=EnhancedMomentumScanRequest)


class TradingChargesUpdateRequest(BaseModel):
    """Editable fields for the master equity-delivery charge schedule."""

    enabled: bool | None = None
    product_type: str | None = None
    exchange: str | None = None
    brokerage_rate_pct: float | None = Field(default=None, ge=0)
    brokerage_cap_rupees: float | None = Field(default=None, ge=0)
    brokerage_min_rupees: float | None = Field(default=None, ge=0)
    stt_buy_pct: float | None = Field(default=None, ge=0)
    stt_sell_pct: float | None = Field(default=None, ge=0)
    transaction_charge_pct: float | None = Field(default=None, ge=0)
    ipft_charge_pct: float | None = Field(default=None, ge=0)
    stamp_duty_buy_pct: float | None = Field(default=None, ge=0)
    sebi_fee_per_crore: float | None = Field(default=None, ge=0)
    dp_charge_per_sell_rupees: float | None = Field(default=None, ge=0)
    gst_pct: float | None = Field(default=None, ge=0)


class StockResponse(BaseModel):
    id: int
    symbol: str
    token: str
    name: str
    company_name: Optional[str] = None
    exchange: str
    series: str
    sector_id: Optional[int] = None
    sector: Optional[str] = None
    industry: str = ""
    market_cap_cr: float = 0
    is_active: bool = True


def _load_stock_display_metadata() -> dict[str, dict[str, Any]]:
    fallback = {
        "APOLLO": {
            "company_name": "Apollo Micro Systems Limited",
            "aliases": ("apollo micro", "apollo micro systems", "apollo microsystems"),
        },
    }
    directory_path = os.path.join(PROJECT_ROOT, "data_cache", "company_directory.json")
    try:
        with open(directory_path, encoding="utf-8") as directory_file:
            payload = json.load(directory_file)
        for symbol, metadata in payload.get("stocks", {}).items():
            company_name = str(metadata.get("company_name", "")).strip()
            if not company_name:
                continue
            fallback.setdefault(
                str(symbol).upper(),
                {
                    "company_name": company_name,
                    "aliases": tuple(str(alias).casefold() for alias in metadata.get("aliases", [])),
                },
            )
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(f"Company directory unavailable; using fallback stock metadata: {exc}")

    # The NSE directory is preferred because it normally contains full legal
    # names. Upstox's cached instrument master fills gaps and also gives us a
    # symbol-to-name source independent of the trading database's short names.
    instrument_dir = Path(PROJECT_ROOT) / "data_cache" / "upstox_instruments"
    for cache_path in sorted(instrument_dir.glob("nse_instruments_*.json"), reverse=True):
        try:
            instruments = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(f"Skipping invalid Upstox instrument cache {cache_path.name}: {exc}")
            continue
        if not isinstance(instruments, list):
            continue
        for instrument in instruments:
            if (
                not isinstance(instrument, dict)
                or instrument.get("segment") != "NSE_EQ"
                or str(instrument.get("instrument_type") or "").upper() != "EQ"
            ):
                continue
            symbol = str(instrument.get("trading_symbol") or "").strip().upper()
            company_name = " ".join(str(instrument.get("name") or "").split())
            if not symbol or not company_name or company_name.casefold() == symbol.casefold():
                continue
            if symbol not in fallback:
                fallback[symbol] = {
                    "company_name": company_name,
                    "aliases": (company_name.casefold(),),
                }
        break
    return fallback


STOCK_DISPLAY_METADATA = _load_stock_display_metadata()


def _stock_base_symbol(symbol: object) -> str:
    base = str(symbol or "").strip().upper()
    for suffix in ("-EQ", "-SM", "-BE", "-BZ", "-ST"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _stock_company_name(symbol: object, stored_name: object = None) -> str | None:
    """Resolve a legal company name without treating a ticker as its name."""
    metadata = STOCK_DISPLAY_METADATA.get(_stock_base_symbol(symbol))
    if metadata:
        company_name = str(metadata.get("company_name") or "").strip()
        if company_name:
            return company_name

    candidate = " ".join(str(stored_name or "").split())
    if not candidate:
        return None
    symbol_key = "".join(char for char in _stock_base_symbol(symbol).casefold() if char.isalnum())
    candidate_symbol = _stock_base_symbol(candidate)
    candidate_metadata = STOCK_DISPLAY_METADATA.get(candidate_symbol)
    if candidate_metadata:
        company_name = str(candidate_metadata.get("company_name") or "").strip()
        if company_name:
            return company_name

    candidate_key = "".join(char for char in candidate_symbol.casefold() if char.isalnum())
    if not candidate_key or candidate_key == symbol_key:
        return None
    # A one-token uppercase value is usually another ticker or an old alias,
    # not a stock's human-readable name.
    if candidate == candidate.upper() and " " not in candidate:
        return None
    return candidate


def _stock_alias_symbols(query: str) -> list[str]:
    normalized = " ".join(query.casefold().replace("-", " ").split())
    if not normalized:
        return []
    query_tokens = normalized.split()
    symbols: list[str] = []
    for symbol, metadata in STOCK_DISPLAY_METADATA.items():
        candidates = (symbol.casefold(), metadata["company_name"].casefold(), *metadata["aliases"])
        if any(
            normalized in candidate or all(token in candidate for token in query_tokens)
            for candidate in candidates
        ):
            symbols.append(f"{symbol}-EQ")
    return symbols


def _serialize_stock_row(row: dict) -> dict:
    serialized = _serialize_row(row)
    company_name = _stock_company_name(row.get("symbol"), row.get("name"))
    if company_name:
        serialized["company_name"] = company_name
    return serialized


def _stock_sector_map(db: Database, stock_ids: list[int]) -> dict[int, str]:
    """Load the latest non-empty Upstox sector for each requested stock."""
    ids = list(dict.fromkeys(int(stock_id) for stock_id in stock_ids if stock_id is not None))
    if not ids:
        return {}

    rows = db.fetch_all(
        """
        SELECT s.id AS stock_id,
               COALESCE(
                   NULLIF(BTRIM(upstox.sector), ''),
                   NULLIF(BTRIM(sec.display_name), ''),
                   NULLIF(BTRIM(sec.name), '')
               ) AS sector
        FROM stocks s
        LEFT JOIN sectors sec ON sec.id = s.sector_id
        LEFT JOIN LATERAL (
            SELECT profile.sector
            FROM company_fundamental_profiles profile
            WHERE profile.stock_id = s.id
              AND NULLIF(BTRIM(profile.sector), '') IS NOT NULL
            ORDER BY profile.snapshot_date DESC NULLS LAST, profile.fetched_at DESC NULLS LAST
            LIMIT 1
        ) upstox ON TRUE
        WHERE s.id = ANY(%s)
        """,
        (ids,),
    )
    return {
        int(row["stock_id"]): str(row["sector"]).strip()
        for row in rows
        if row.get("sector") and str(row["sector"]).strip()
    }


def _attach_stock_sectors(
    db: Database,
    rows: list[dict[str, Any]],
    stock_id_key: str = "stock_id",
) -> list[dict[str, Any]]:
    """Add the sector field to stock rows without confusing it with industry."""
    ids = [row.get(stock_id_key, row.get("id")) for row in rows]
    sectors = _stock_sector_map(db, [stock_id for stock_id in ids if stock_id is not None])
    for row in rows:
        stock_id = row.get(stock_id_key, row.get("id"))
        try:
            sector = sectors.get(int(stock_id)) if stock_id is not None else None
        except (TypeError, ValueError):
            sector = None
        # Keep an explicitly selected sector value from a query as fallback;
        # industry stays a separate classification and is never relabeled.
        row["sector"] = sector or row.get("sector") or row.get("sector_display") or row.get("sector_name")
    return rows


def _attach_stock_display_metadata_by_symbol(
    db: Database,
    rows: list[dict[str, Any]],
    symbol_key: str = "symbol",
) -> list[dict[str, Any]]:
    """Hydrate stock name and sector on persisted results that only store a ticker."""
    symbols = list(dict.fromkeys(
        str(row.get(symbol_key) or "").strip().upper()
        for row in rows
        if str(row.get(symbol_key) or "").strip()
    ))
    stock_rows = db.fetch_all(
        "SELECT id, symbol, name FROM stocks WHERE UPPER(symbol) = ANY(%s)",
        (symbols,),
    ) if symbols else []
    stock_by_symbol = {str(row["symbol"]).upper(): row for row in stock_rows}
    sectors = _stock_sector_map(db, [row["id"] for row in stock_rows])
    for row in rows:
        stock = stock_by_symbol.get(str(row.get(symbol_key) or "").strip().upper())
        if stock:
            row["company_name"] = _stock_company_name(stock["symbol"], stock.get("name")) or ""
            row["sector"] = sectors.get(int(stock["id"]))
        else:
            row.setdefault("company_name", "")
            row.setdefault("sector", None)
    return rows


_SECTOR_SESSION_OFFSETS = {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "1y": 252}


def _canonical_sector_name(value: object) -> str:
    """Group the provider's fine-grained industry labels into investable sectors."""
    raw = str(value or "").strip()
    normalized = re.sub(r"[^a-z0-9]", "", raw.casefold())
    if not normalized:
        return "Other / Unclassified"
    if normalized in {"dvr", "miscellaneous", "unspecified", "other", "unclassified"}:
        return "Other / Unclassified"
    if normalized.startswith(("bank", "nbfc", "financialservices", "finance", "investment", "stockbrok", "stockcommoditybrok", "assetmanagement", "housingfinance", "rating")):
        return "Banking & Financial Services"
    if normalized.startswith("insurance"):
        return "Insurance"
    if normalized.startswith(("informationtechnology", "infotech", "software", "itsoftware", "itservices", "ithardware", "iteducation", "itnetworking", "bpoites")) or normalized == "it":
        return "Information Technology"
    if normalized.startswith(("pharma", "pharmaceutical")):
        return "Pharmaceuticals"
    if normalized.startswith(("healthcare", "hospital", "medical", "diagnostic")):
        return "Healthcare Services & Equipment"
    if normalized.startswith(("automobile", "autocomponent", "automotive", "autoancillar", "tyre", "cycle")):
        return "Automobiles & Components"
    if normalized.startswith(("realty", "realestate")):
        return "Real Estate"
    if normalized.startswith(("battery", "batteries", "electricvehicle")):
        return "EV & Batteries"
    if normalized.startswith(("energy", "power", "electricity", "renewableenergy", "powergeneration", "oilexploration", "oildrill", "crudeoil", "naturalgas", "refineries", "gastransmission", "gasesfuels")):
        return "Energy & Utilities"
    if normalized.startswith(("metal", "mining", "steel", "aluminium", "nonferrous", "minerals")):
        return "Metals & Mining"
    if normalized.startswith(("textile", "textiles", "garment", "apparel", "fabric", "yarn", "cotton")):
        return "Textiles & Apparel"
    if normalized.startswith(("fmcg", "consumerstaple", "consumerfood", "fastmovingconsumergoods", "householdproducts", "teacoffee", "breweries", "alcoholicbeverages", "edibleoil", "tobacco")):
        return "Consumer Staples & Food"
    if normalized.startswith(("sugar", "distilleries")):
        return "Sugar & Distilleries"
    if normalized.startswith(("fertilizer", "fertiliser", "agrochemical", "agrichemical", "agriculture", "aquaculture", "agriinput")):
        return "Agriculture & Fertilizers"
    if normalized.startswith(("cement", "buildingmaterial", "constructionmaterial", "ceramics", "refractories", "glass", "plywood", "decoratives", "tiles")):
        return "Cement & Building Materials"
    if normalized.startswith(("construction", "infrastructure", "powerinfrastructure", "transmissiontower")):
        return "Construction & Infrastructure"
    if normalized.startswith(("chemical", "dyespigments", "paint", "petrochemical", "carbonblack", "plasticproducts", "rubberproducts", "lubricants", "solventextraction")):
        return "Chemicals & Materials"
    if normalized.startswith(("it", "electronics", "electric", "capitalgoods", "engineering", "compressor", "bearing", "casting", "forging", "fastener", "welding", "dieselengine", "cable", "machinery", "industrial", "abrasives")):
        return "Industrials & Capital Goods"
    if normalized.startswith(("telecom", "telecommunication")):
        return "Telecommunications"
    if normalized.startswith(("media", "entertainment", "filmproduction", "tvbroadcasting", "printingandpublishing")):
        return "Media & Entertainment"
    if normalized.startswith(("defence", "defense", "aerospace", "shipbuilding")):
        return "Defence & Aerospace"
    if normalized.startswith(("railway", "railways", "rail")):
        return "Railways"
    if normalized.startswith(("logistics", "shipping", "port", "courier", "airlines", "transport")):
        return "Transport & Logistics"
    if normalized.startswith(("hotel", "travel", "recreation", "quickservicerestaurant", "restaurant", "hospitality")):
        return "Travel & Hospitality"
    if normalized.startswith(("retailing", "retail", "consumerdurable", "airconditioner", "jewellery", "diamondgem", "ecommerce", "watches", "footwear", "leather", "coworking")):
        return "Consumer & Retail"
    if normalized.startswith(("paper", "packaging", "stationery", "woodproducts")):
        return "Paper, Packaging & Products"
    if normalized.startswith(("education", "educationalinstitution")):
        return "Education"
    if normalized.startswith("trading"):
        return "Trading & Distribution"
    if normalized.startswith("diversified"):
        return "Diversified"
    return raw


def _sector_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _canonical_sector_name(value).casefold()).strip("-")


def _load_sector_market_rows(db: Database) -> list[dict[str, Any]]:
    """Read one recent daily-candle window and latest indicators for NSE/BSE equities."""
    return db.fetch_all(
        f"""
        SELECT s.id AS stock_id, s.symbol, s.name, s.market_cap_cr, s.industry,
               COALESCE(NULLIF(BTRIM(upstox.sector), ''),
                        NULLIF(BTRIM(sec.display_name), ''),
                        NULLIF(BTRIM(sec.name), ''),
                        NULLIF(BTRIM(s.industry), '')) AS raw_sector,
               prices.latest_at, prices.latest_close, prices.close_1w,
               prices.close_1m, prices.close_3m, prices.close_6m, prices.close_1y,
               ti.rsi_14, ti.sma_20, ti.sma_50, ti.sma_200,
               ti.macd_line, ti.macd_signal, ti.adx_14,
               ti.date AS indicator_date
        FROM stocks s
        LEFT JOIN sectors sec ON sec.id = s.sector_id
        LEFT JOIN LATERAL (
            SELECT profile.sector
            FROM company_fundamental_profiles profile
            WHERE profile.stock_id = s.id
              AND NULLIF(BTRIM(profile.sector), '') IS NOT NULL
            ORDER BY profile.snapshot_date DESC NULLS LAST, profile.fetched_at DESC NULLS LAST
            LIMIT 1
        ) upstox ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(timestamp) FILTER (WHERE session_rank = 1) AS latest_at,
                   MAX(close) FILTER (WHERE session_rank = 1) AS latest_close,
                   MAX(close) FILTER (WHERE session_rank = 6) AS close_1w,
                   MAX(close) FILTER (WHERE session_rank = 22) AS close_1m,
                   MAX(close) FILTER (WHERE session_rank = 64) AS close_3m,
                   MAX(close) FILTER (WHERE session_rank = 127) AS close_6m,
                   MAX(close) FILTER (WHERE session_rank = 253) AS close_1y
            FROM (
                SELECT recent.timestamp, recent.close,
                       ROW_NUMBER() OVER (ORDER BY recent.timestamp DESC) AS session_rank
                FROM (
                    SELECT candle.timestamp, candle.close
                    FROM candles_1d candle
                    WHERE candle.stock_id = s.id
                      AND candle.timestamp >= CURRENT_DATE - INTERVAL '730 days'
                    ORDER BY candle.timestamp DESC
                    LIMIT 253
                ) recent
            ) ranked
        ) prices ON TRUE
        LEFT JOIN LATERAL (
            SELECT date, rsi_14, sma_20, sma_50, sma_200, macd_line, macd_signal, adx_14
            FROM technical_indicators
            WHERE stock_id = s.id
            ORDER BY date DESC LIMIT 1
        ) ti ON TRUE
        WHERE {equity_universe_sql('s')}
        ORDER BY s.symbol
        """
    )


def _return_pct(latest: object, previous: object) -> float | None:
    try:
        current_value = float(latest) if latest is not None else 0.0
        previous_value = float(previous) if previous is not None else 0.0
    except (TypeError, ValueError):
        return None
    if current_value <= 0 or previous_value <= 0:
        return None
    return ((current_value / previous_value) - 1) * 100


def _build_sector_analysis(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], str | None]:
    groups: dict[str, list[dict[str, Any]]] = {}
    latest_at = None
    for row in rows:
        label = _canonical_sector_name(row.get("raw_sector"))
        key = _sector_key(label)
        row["sector"] = label
        row["sector_key"] = key
        row["returns"] = {
            period: _return_pct(row.get("latest_close"), row.get(f"close_{period}"))
            for period in _SECTOR_SESSION_OFFSETS
        }
        groups.setdefault(key, []).append(row)
        candle_date = row.get("latest_at")
        if candle_date and (latest_at is None or candle_date > latest_at):
            latest_at = candle_date

    summaries = []
    for key, members in groups.items():
        priced = [member for member in members if member.get("latest_close") is not None]
        if not priced:
            continue
        periods = {}
        for period in _SECTOR_SESSION_OFFSETS:
            values = [member["returns"][period] for member in priced if member["returns"][period] is not None]
            periods[period] = {
                "return_pct": round(sum(values) / len(values), 2) if values else None,
                "sample_count": len(values),
                "positive_count": sum(value > 0 for value in values),
            }
        month_values = [member["returns"]["1m"] for member in priced if member["returns"]["1m"] is not None]
        rsi_values = [float(member["rsi_14"]) for member in priced if member.get("rsi_14") is not None]
        above_sma200 = [
            float(member["latest_close"]) > float(member["sma_200"])
            for member in priced
            if member.get("sma_200") is not None
        ]
        summary = {
            "key": key,
            "name": members[0]["sector"],
            "stock_count": len(members),
            "priced_stock_count": len(priced),
            "periods": periods,
            "advancing_1m": sum(value > 0 for value in month_values),
            "declining_1m": sum(value < 0 for value in month_values),
            "advance_pct_1m": round(sum(value > 0 for value in month_values) / len(month_values) * 100, 1) if month_values else None,
            "average_rsi_14": round(sum(rsi_values) / len(rsi_values), 1) if rsi_values else None,
            "above_sma200_pct": round(sum(above_sma200) / len(above_sma200) * 100, 1) if above_sma200 else None,
        }
        month_return = periods["1m"]["return_pct"]
        quarter_return = periods["3m"]["return_pct"]
        half_year_return = periods["6m"]["return_pct"]
        breadth = summary["advance_pct_1m"]
        if quarter_return is not None and quarter_return < 0 and half_year_return is not None and half_year_return < 0 and month_return is not None and month_return > 0 and breadth is not None and breadth >= 55:
            summary["outlook"] = "Early recovery"
            summary["outlook_detail"] = "The latest month and market breadth have improved after a longer decline. This is an early improvement signal; confirmation is still needed."
        elif month_return is not None and quarter_return is not None and month_return > 0 and quarter_return > 0 and breadth is not None and breadth >= 55:
            summary["outlook"] = "Broadening strength"
            summary["outlook_detail"] = "Positive short and medium-term returns are supported by broad participation across the sector."
        elif month_return is not None and month_return < 0 and quarter_return is not None and quarter_return < 0:
            summary["outlook"] = "Weak trend"
            summary["outlook_detail"] = "Recent and medium-term returns are negative. A prolonged decline alone is not evidence that a rebound is due."
        elif month_return is not None and month_return < 0 and quarter_return is not None and quarter_return > 0:
            summary["outlook"] = "Short-term cooling"
            summary["outlook_detail"] = "The medium-term trend remains positive, while the latest month has weakened. Watch whether breadth stabilizes."
        elif month_return is not None and month_return > 0 and breadth is not None and breadth < 45:
            summary["outlook"] = "Narrow advance"
            summary["outlook_detail"] = "The sector average is rising, but fewer than half of its stocks advanced over the month. Participation is limited."
        else:
            summary["outlook"] = "Mixed signals"
            summary["outlook_detail"] = "Returns and participation do not yet point in the same direction. Use the individual stock evidence below."
        summaries.append(summary)

    summaries.sort(key=lambda item: item["periods"]["1m"]["return_pct"] if item["periods"]["1m"]["return_pct"] is not None else float("-inf"), reverse=True)
    as_of_date = latest_at.date().isoformat() if latest_at and hasattr(latest_at, "date") else (str(latest_at) if latest_at else None)
    return summaries, groups, as_of_date


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
    def convert(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        if hasattr(value, "__float__") and not isinstance(value, (str, bytes, bool)):
            return float(value)
        return value

    serialized = {key: convert(value) for key, value in row.items()}
    if serialized.get("symbol") is not None and "name" in serialized:
        company_name = _stock_company_name(serialized["symbol"], serialized.get("name"))
        if company_name:
            serialized["company_name"] = company_name
    return serialized


def _resolve_stock_id(
    db: Database,
    symbol: str,
    stock_id: int | None = None,
) -> dict | None:
    """
    Find a stock by symbol, handling the '-EQ' suffix convention.

    Tries in order:
        1. Exact match (e.g. 'ADANIPORTS-EQ')
        2. With '-EQ' appended (e.g. 'ADANIPORTS' → 'ADANIPORTS-EQ')
        3. Case-insensitive ILIKE fallback
    """
    sym = symbol.upper()
    if stock_id is not None:
        stock = db.fetch_one("SELECT * FROM stocks WHERE id = %s", (stock_id,))
        if stock and _stock_base_symbol(stock.get("symbol")) == _stock_base_symbol(sym):
            return stock
        return None
    # Prefer NSE for an ambiguous symbol, but allow a BSE-only stock that is
    # already part of a saved watchlist to be charted and backfilled.
    # psycopg serializes lists to PostgreSQL arrays; a tuple is a SQL record
    # and cannot be used by ANY(...).
    exchanges = ["NSE", "BSE"]
    # Try exact match first
    stock = db.fetch_one(
        """
        SELECT * FROM stocks
        WHERE symbol = %s AND exchange = ANY(%s)
        ORDER BY CASE exchange WHEN 'NSE' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (sym, exchanges),
    )
    if stock:
        return stock

    # Try with -EQ suffix
    if not sym.endswith("-EQ"):
        stock = db.fetch_one(
            """
            SELECT * FROM stocks
            WHERE symbol = %s AND exchange = ANY(%s)
            ORDER BY CASE exchange WHEN 'NSE' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (f"{sym}-EQ", exchanges),
        )
        if stock:
            return stock

    # Fuzzy fallback
    stock = db.fetch_one(
        """
        SELECT * FROM stocks
        WHERE symbol ILIKE %s AND exchange = ANY(%s)
        ORDER BY CASE exchange WHEN 'NSE' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (f"{sym}%", exchanges),
    )
    return stock


_FILTER_CATALOG = build_default_catalog()
_PREVIEW_TABLES = {
    "1m": "candles_1m",
    "5m": "candles_5m",
    "15m": "candles_15m",
    "1h": "candles_1h",
    "1d": "candles_1d",
    "1w": "candles_1w",
}


def _parse_filter_expression(request: FilterRequirementsRequest) -> tuple[FilterExpression | None, dict, list[str]]:
    """Validate a user-supplied expression and calculate its data contract."""
    errors: list[str] = []
    try:
        expression = FilterExpression.model_validate(request.expression)
        _FILTER_CATALOG.validate_expression(expression, request.mode)
    except Exception as exc:
        return None, {}, [str(exc).strip("'")]

    requirements: dict[str, dict[str, Any]] = {}

    def visit(node):
        if isinstance(node, FilterGroup):
            for child in node.children:
                visit(child)
            return
        requirement = _FILTER_CATALOG.requirements_for(
            node.filter_id,
            node.params,
            timeframe=node.timeframe,
        )
        key = node.timeframe.value
        current = requirements.setdefault(
            key,
            {
                "minimum_candles": 0,
                "warmup": 0,
                "required_fields": [],
                "requires_live": False,
            },
        )
        current["minimum_candles"] = max(current["minimum_candles"], requirement.minimum_candles)
        current["warmup"] = max(current["warmup"], requirement.warmup)
        current["required_fields"] = sorted(set(current["required_fields"]) | set(requirement.required_fields))
        current["requires_live"] = current["requires_live"] or requirement.requires_live

    for node in expression.children + expression.veto:
        visit(node)
    return expression, requirements, errors


def _json_profile(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_profile(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_profile(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "__float__") and not isinstance(value, (str, bytes, bool)):
        return float(value)
    return value


def _clamp_momentum_score(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _return_component_score(value: Any, floor: float, ceiling: float) -> float | None:
    """Map a price return into a bounded score without an index benchmark."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return _clamp_momentum_score((parsed - floor) / (ceiling - floor) * 100)


def _momentum_scan_score(row: dict[str, Any]) -> dict[str, Any]:
    """Score an NSE stock using price, trend, volume and breakout evidence."""
    price_components = (
        ("return_20d", 0.20, -10, 25),
        ("return_60d", 0.30, -20, 60),
        ("return_120d", 0.25, -30, 100),
        ("return_252d", 0.25, -50, 180),
    )
    weighted_returns = []
    for field_name, weight, floor, ceiling in price_components:
        component = _return_component_score(row.get(field_name), floor, ceiling)
        if component is not None:
            weighted_returns.append((component, weight))
    price_score = (
        sum(component * weight for component, weight in weighted_returns) / sum(weight for _, weight in weighted_returns)
        if weighted_returns
        else 0.0
    )

    trend_checks = []
    close = _number(row.get("latest_close"))
    sma_20 = _number(row.get("sma_20"))
    sma_50 = _number(row.get("sma_50"))
    sma_200 = _number(row.get("sma_200"))
    high_52w = _number(row.get("high_52w"))
    if close > 0 and sma_20 > 0:
        trend_checks.append(close > sma_20)
    if sma_20 > 0 and sma_50 > 0:
        trend_checks.append(sma_20 > sma_50)
    if sma_50 > 0 and sma_200 > 0:
        trend_checks.append(sma_50 > sma_200)
    if close > 0 and high_52w > 0:
        trend_checks.append(close >= high_52w * 0.95)
    trend_score = sum(trend_checks) / len(trend_checks) * 100 if trend_checks else 0.0

    volume_ratio = _number(row.get("volume_ratio"))
    avg_turnover_cr = _number(row.get("avg_turnover_20d_cr"))
    volume_score = (
        0.65 * _clamp_momentum_score((volume_ratio - 1) / 2 * 100)
        + 0.35 * _clamp_momentum_score(avg_turnover_cr / 10 * 100)
    )
    breakout_score = (
        _clamp_momentum_score((close / high_52w - 0.75) / 0.25 * 100)
        if close > 0 and high_52w > 0
        else 0.0
    )
    score = 0.45 * price_score + 0.30 * trend_score + 0.20 * volume_score + 0.05 * breakout_score
    if score >= 75 and volume_ratio >= 1.5:
        category = "Strong momentum"
    elif score >= 60:
        category = "Good momentum"
    elif volume_ratio >= 2:
        category = "Volume surge"
    else:
        category = "Building momentum"
    return {
        **row,
        "momentum_score": round(score, 2),
        "price_momentum_score": round(price_score, 2),
        "trend_score": round(trend_score, 2),
        "volume_score": round(volume_score, 2),
        "breakout_score": round(breakout_score, 2),
        "category": category,
        "trend_aligned": bool(close > sma_20 > sma_50 > 0) if sma_20 and sma_50 else False,
    }


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
        alias_symbols = _stock_alias_symbols(q)
        alias_clause = " OR s.symbol = ANY(%s)" if alias_symbols else ""
        order_alias_clause = " WHEN s.symbol = ANY(%s) THEN 1" if alias_symbols else ""
        params: tuple[Any, ...]
        if alias_symbols:
            params = (
                exchange,
                f"%{q}%",
                f"%{q}%",
                alias_symbols,
                f"{q}%",
                alias_symbols,
                limit,
                offset,
            )
        else:
            params = (exchange, f"%{q}%", f"%{q}%", f"{q}%", limit, offset)
        rows = db.fetch_all(
            f"""
            SELECT s.id, s.symbol, s.token, s.name, s.exchange, s.series,
                   s.sector_id, s.industry, s.market_cap_cr, s.is_active,
                   lc.close AS latest_price
            FROM stocks s
            LEFT JOIN LATERAL (
                SELECT close FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT 1
            ) lc ON TRUE
            WHERE {equity_universe_sql('s')}
              AND s.exchange = %s
              AND (s.symbol ILIKE %s OR s.name ILIKE %s{alias_clause})
            ORDER BY
                CASE WHEN s.symbol ILIKE %s THEN 0{order_alias_clause} ELSE 2 END,
                s.symbol
            LIMIT %s OFFSET %s
            """,
            params,
        )
    else:
        rows = db.fetch_all(
            f"""
            SELECT s.id, s.symbol, s.token, s.name, s.exchange, s.series,
                   s.sector_id, s.industry, s.market_cap_cr, s.is_active,
                   lc.close AS latest_price
            FROM stocks s
            LEFT JOIN LATERAL (
                SELECT close FROM candles_1d
                WHERE stock_id = s.id ORDER BY timestamp DESC LIMIT 1
            ) lc ON TRUE
            WHERE {equity_universe_sql('s')} AND s.exchange = %s
            ORDER BY s.symbol
            LIMIT %s OFFSET %s
            """,
            (exchange, limit, offset),
        )
    _attach_stock_sectors(db, rows, stock_id_key="id")
    return {"stocks": [_serialize_stock_row(r) for r in rows], "count": len(rows)}


class StockCompanyNamesRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=5000)
    stock_ids: list[int] = Field(default_factory=list, max_length=5000)


class FundamentalScoreInputsRequest(BaseModel):
    stock_ids: list[int] = Field(min_length=1, max_length=5000)


@app.post("/api/stocks/company-names")
def get_stock_company_names(request: StockCompanyNamesRequest):
    """Resolve company names and sectors for already-saved scanner results."""
    names = {}
    for symbol in dict.fromkeys(request.symbols):
        company_name = _stock_company_name(symbol)
        if company_name:
            names[symbol] = company_name
    names_by_stock_id = {}
    symbols = list(dict.fromkeys(
        _stock_base_symbol(symbol)
        for symbol in request.symbols
        if str(symbol or "").strip()
    ))
    stock_ids = list(dict.fromkeys(request.stock_ids))
    db = get_db()
    rows = db.fetch_all(
        f"""
        SELECT id, symbol, name, exchange
        FROM stocks
        WHERE id = ANY(%s) OR (
            {equity_universe_sql('stocks')}
            AND REGEXP_REPLACE(UPPER(BTRIM(stocks.symbol)), '-(EQ|SM|BE|BZ|ST)$', '') = ANY(%s)
        )
        ORDER BY
            CASE WHEN exchange = 'NSE' THEN 0 ELSE 1 END,
            CASE WHEN COALESCE(series, 'EQ') = 'EQ' THEN 0 ELSE 1 END,
            CASE WHEN RIGHT(UPPER(symbol), 3) = '-EQ' THEN 0 ELSE 1 END,
            id
        """,
        (stock_ids, symbols),
    ) if stock_ids or symbols else []
    sectors_by_stock_id = _stock_sector_map(db, [row["id"] for row in rows])
    sectors_by_symbol = {}
    stock_ids_by_symbol = {}
    for row in rows:
        raw_symbol_key = str(row.get("symbol") or "").strip().upper()
        symbol_key = _stock_base_symbol(raw_symbol_key)
        if raw_symbol_key:
            stock_ids_by_symbol.setdefault(raw_symbol_key, int(row["id"]))
        if symbol_key:
            stock_ids_by_symbol.setdefault(symbol_key, int(row["id"]))
        company_name = _stock_company_name(row.get("symbol"), row.get("name"))
        if company_name:
            names_by_stock_id[str(row["id"])] = company_name
        sector = sectors_by_stock_id.get(int(row["id"]))
        if sector:
            sectors_by_symbol[row["symbol"]] = sector
    return {
        "company_names": names,
        "company_names_by_stock_id": names_by_stock_id,
        "stock_ids_by_symbol": stock_ids_by_symbol,
        "sectors": sectors_by_symbol,
        "sectors_by_stock_id": {str(stock_id): sector for stock_id, sector in sectors_by_stock_id.items()},
    }


@app.get("/api/sectors/analysis")
def get_sector_analysis():
    """Summarize equal-weight sector returns and breadth over recent trading sessions."""
    rows = _load_sector_market_rows(get_db())
    sectors, _, as_of_date = _build_sector_analysis(rows)
    return {
        "as_of_date": as_of_date,
        "universe_stocks": len(rows),
        "priced_stocks": sum(1 for row in rows if row.get("latest_close") is not None),
        "return_method": "Equal-weight average of member stock returns",
        "period_sessions": _SECTOR_SESSION_OFFSETS,
        "sectors": sectors,
    }


@app.get("/api/sectors/{sector_key}/stocks")
def get_sector_stocks(
    sector_key: str,
    limit: int = Query(2000, ge=1, le=5000),
):
    """Return priced constituents and their latest technical indicator snapshot."""
    rows = _load_sector_market_rows(get_db())
    _, groups, _ = _build_sector_analysis(rows)
    members = groups.get(sector_key, [])
    priced = [row for row in members if row.get("latest_close") is not None]
    priced.sort(
        key=lambda row: (float(row.get("market_cap_cr") or 0), str(row.get("symbol") or "")),
        reverse=True,
    )
    result = []
    for row in priced[:limit]:
        result.append({
            "stock_id": int(row["stock_id"]),
            "symbol": row["symbol"],
            "name": _stock_company_name(row["symbol"], row.get("name")) or row.get("name") or row["symbol"],
            "market_cap_cr": row.get("market_cap_cr"),
            "latest_close": row.get("latest_close"),
            "latest_at": row.get("latest_at"),
            "returns": row["returns"],
            "technical": {
                "date": row.get("indicator_date"),
                "rsi_14": row.get("rsi_14"),
                "sma_20": row.get("sma_20"),
                "sma_50": row.get("sma_50"),
                "sma_200": row.get("sma_200"),
                "macd_line": row.get("macd_line"),
                "macd_signal": row.get("macd_signal"),
                "adx_14": row.get("adx_14"),
            },
        })
    return _serialize_row({
        "sector_key": sector_key,
        "sector_name": members[0]["sector"] if members else None,
        "total_stocks": len(members),
        "priced_stocks": len(priced),
        "returned_stocks": len(result),
        "unpriced_stocks": len(members) - len(priced),
        "stocks": result,
    })


@app.post("/api/momentum-scanner/scan")
def scan_momentum_universe(request: MomentumScanRequest):
    """Rank active NSE and BSE equity stocks using one daily-candle aggregation."""
    db = get_db()
    as_of = _last_completed_daily_scan_date(request.as_of_date or datetime.now(IST).date())
    end_exclusive = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    # 560 calendar days normally covers at least 252 NSE sessions, including
    # weekends and exchange holidays, while keeping the universe query small.
    start_inclusive = end_exclusive - timedelta(days=560)
    rows = db.fetch_all(
        f"""
        WITH universe AS (
            SELECT id, symbol, name, market_cap_cr, industry,
                   COUNT(*) OVER () AS universe_count
            FROM stocks
            WHERE {equity_universe_sql('stocks')}
        ), {_CANONICAL_DAILY_SESSION_CTES}, ranked_candles AS (
            SELECT
                daily.stock_id,
                daily.timestamp,
                daily.session_date,
                daily.high,
                daily.low,
                daily.close,
                daily.volume,
                daily.delivery_pct,
                ROW_NUMBER() OVER (
                    PARTITION BY daily.stock_id ORDER BY daily.session_date DESC
                ) AS rn
            FROM daily_sessions daily
        ), summary AS (
            SELECT
                stock_id,
                MAX(timestamp) FILTER (WHERE rn = 1) AS latest_timestamp,
                MAX(close) FILTER (WHERE rn = 1) AS latest_close,
                MAX(volume) FILTER (WHERE rn = 1) AS latest_volume,
                MAX(delivery_pct) FILTER (WHERE rn = 1) AS latest_delivery_pct,
                MAX(close) FILTER (WHERE rn = 21) AS close_20d,
                MAX(close) FILTER (WHERE rn = 61) AS close_60d,
                MAX(close) FILTER (WHERE rn = 121) AS close_120d,
                MAX(close) FILTER (WHERE rn = 253) AS close_252d,
                AVG(close) FILTER (WHERE rn BETWEEN 1 AND 20) AS sma_20,
                AVG(close) FILTER (WHERE rn BETWEEN 1 AND 50) AS sma_50,
                AVG(close) FILTER (WHERE rn BETWEEN 1 AND 200) AS sma_200,
                AVG(volume) FILTER (WHERE rn BETWEEN 2 AND 21) AS prior_20d_volume,
                AVG(close * volume) FILTER (WHERE rn BETWEEN 1 AND 20) / 10000000.0 AS avg_turnover_20d_cr,
                AVG(delivery_pct) FILTER (WHERE rn BETWEEN 1 AND 20) AS avg_delivery_pct_20d,
                MAX(high) FILTER (WHERE rn BETWEEN 1 AND 252) AS high_52w,
                AVG((high - low) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 20) AS avg_range_pct_20d,
                COUNT(*) AS observations
            FROM ranked_candles
            GROUP BY stock_id
        )
        SELECT
            u.id AS stock_id,
            u.symbol,
            u.name,
            u.exchange,
            u.industry,
            u.market_cap_cr,
            u.universe_count,
            s.latest_timestamp,
            s.latest_close,
            s.latest_volume,
            s.latest_delivery_pct,
            s.close_20d,
            s.close_60d,
            s.close_120d,
            s.close_252d,
            s.sma_20,
            s.sma_50,
            s.sma_200,
            s.prior_20d_volume,
            s.avg_turnover_20d_cr,
            s.avg_delivery_pct_20d,
            s.high_52w,
            s.avg_range_pct_20d,
            s.observations,
            CASE
                WHEN COALESCE(s.prior_20d_volume, 0) > 0
                THEN s.latest_volume / s.prior_20d_volume
                ELSE 0
            END AS volume_ratio
        FROM summary s
        JOIN universe u ON u.id = s.stock_id
        WHERE s.latest_close IS NOT NULL
          AND s.observations >= 60
        """,
        (start_inclusive, end_exclusive),
    )

    scored: list[dict[str, Any]] = []
    universe_count = int(_number(rows[0].get("universe_count"))) if rows else 0
    for row in rows:
        item = {key: value for key, value in row.items()}
        latest_close = _number(item.get("latest_close"))
        high_52w = _number(item.get("high_52w"))
        for field_name, close_field in (
            ("return_20d", "close_20d"),
            ("return_60d", "close_60d"),
            ("return_120d", "close_120d"),
            ("return_252d", "close_252d"),
        ):
            prior_close = _number(item.get(close_field))
            item[field_name] = round((latest_close / prior_close - 1) * 100, 2) if latest_close and prior_close else None
        scored_item = _momentum_scan_score(item)
        if scored_item["momentum_score"] < request.min_score:
            continue
        if _number(scored_item.get("latest_close")) < request.min_price:
            continue
        if _number(scored_item.get("avg_turnover_20d_cr")) < request.min_avg_turnover_cr:
            continue
        if _number(scored_item.get("volume_ratio")) < request.min_volume_ratio:
            continue
        if scored_item.get("return_20d") is None or scored_item["return_20d"] < request.min_return_20d:
            continue
        if scored_item.get("return_60d") is None or scored_item["return_60d"] < request.min_return_60d:
            continue
        if _number(scored_item.get("avg_delivery_pct_20d")) < request.min_delivery_pct:
            continue
        if request.trend_mode == "aligned" and not scored_item["trend_aligned"]:
            continue
        if request.trend_mode == "above_sma50":
            sma_50 = _number(scored_item.get("sma_50"))
            if not latest_close or not sma_50 or latest_close <= sma_50:
                continue

        result = {
            "stock_id": scored_item["stock_id"],
            "symbol": scored_item["symbol"],
            "name": scored_item["name"],
            "exchange": scored_item.get("exchange"),
            "company_name": _stock_company_name(scored_item["symbol"], scored_item["name"]) or "",
            "industry": scored_item.get("industry") or "",
            "market_cap_cr": round(_number(scored_item.get("market_cap_cr")), 2),
            "as_of_date": (
                _enhanced_backtest_date(scored_item.get("latest_timestamp")).isoformat()
                if _enhanced_backtest_date(scored_item.get("latest_timestamp"))
                else as_of.isoformat()
            ),
            "close": round(latest_close, 2),
            "return_20d": scored_item["return_20d"],
            "return_60d": scored_item["return_60d"],
            "return_120d": scored_item["return_120d"],
            "return_252d": scored_item["return_252d"],
            "volume_ratio": round(_number(scored_item.get("volume_ratio")), 2),
            "avg_turnover_20d_cr": round(_number(scored_item.get("avg_turnover_20d_cr")), 2),
            "latest_delivery_pct": round(_number(scored_item.get("latest_delivery_pct")), 2),
            "avg_delivery_pct_20d": round(_number(scored_item.get("avg_delivery_pct_20d")), 2),
            "sma_20": round(_number(scored_item.get("sma_20")), 2),
            "sma_50": round(_number(scored_item.get("sma_50")), 2),
            "sma_200": round(_number(scored_item.get("sma_200")), 2),
            "high_52w": round(high_52w, 2),
            "distance_from_52w_high_pct": round((latest_close / high_52w - 1) * 100, 2) if latest_close and high_52w else None,
            "avg_range_pct_20d": round(_number(scored_item.get("avg_range_pct_20d")), 2),
            "observations": int(_number(scored_item.get("observations"))),
            "momentum_score": scored_item["momentum_score"],
            "price_momentum_score": scored_item["price_momentum_score"],
            "trend_score": scored_item["trend_score"],
            "volume_score": scored_item["volume_score"],
            "breakout_score": scored_item["breakout_score"],
            "trend_aligned": scored_item["trend_aligned"],
            "category": scored_item["category"],
        }
        scored.append(result)

    scored.sort(key=lambda item: (
        -item["momentum_score"],
        -item["volume_ratio"],
        item["symbol"],
        item.get("exchange") or "",
    ))
    for rank, item in enumerate(scored[: request.max_results], start=1):
        item["rank"] = rank
    results = _attach_stock_sectors(db, scored[: request.max_results])
    return {
        "as_of_date": as_of.isoformat(),
        "universe": "NSE and BSE company equities (ETFs and index instruments excluded)",
        "universe_stocks": universe_count,
        "scanned_stocks": len(rows),
        "matching_stocks": len(scored),
        "returned_stocks": len(results),
        "filters": request.model_dump(mode="json"),
        "formula": {
            "score": "45% price momentum + 30% trend alignment + 20% volume/liquidity confirmation + 5% 52-week breakout proximity",
            "price_momentum": "20d, 60d, 120d and 252d returns, each bounded and re-weighted when history is unavailable",
            "trend_alignment": "close > SMA20 > SMA50, with SMA50 > SMA200 and proximity to the 52-week high contributing to the score",
            "volume_confirmation": "latest volume ÷ previous 20-session average volume, combined with 20-session average traded value",
            "not_an_index": "No Nifty, Sensex or index constituent membership is used in the score or universe.",
            "instrument_universe": "Active NSE and BSE company equities; ETFs and index instruments are excluded from the stock universe.",
        },
        "results": results,
    }


def _enhanced_benchmark_candidates(db) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve index instruments without assuming one broker symbol format."""
    rows = db.fetch_all(
        """
        SELECT id, symbol, name, exchange, token
        FROM stocks
        WHERE is_active = TRUE
          AND (
              token LIKE '999%%'
              OR UPPER(symbol) IN ('NIFTY', 'NIFTY50', 'NIFTY 50', 'SENSEX', 'BSESN')
              OR UPPER(name) LIKE '%%NIFTY 50%%'
              OR UPPER(name) LIKE '%%SENSEX%%'
          )
        ORDER BY symbol
        """
    )

    def choose(kind: str) -> dict[str, Any] | None:
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper().replace("-EQ", "")
            name = str(row.get("name") or "").upper()
            token = str(row.get("token") or "")
            score = 0
            if kind == "nifty":
                if symbol in {"NIFTY", "NIFTY50", "NIFTY 50"}:
                    score += 100
                if "NIFTY 50" in name or "NIFTY50" in name:
                    score += 80
                if "NIFTY" in symbol and "BANK" not in symbol and "NEXT" not in symbol:
                    score += 20
            else:
                if symbol in {"SENSEX", "BSESN"}:
                    score += 100
                if "SENSEX" in name:
                    score += 80
            if token.startswith("999"):
                score += 10
            if score:
                scored.append((score, row))
        return max(scored, key=lambda item: item[0])[1] if scored else None

    return choose("nifty"), choose("sensex")


def _load_enhanced_daily_candles(db, stock_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT timestamp, open, high, low, close, volume
        FROM candles_1d
        WHERE stock_id = %s AND timestamp >= %s AND timestamp < %s
        ORDER BY timestamp ASC
        """,
        (stock_id, start, end),
    )
    normalized, quality = normalize_daily_candles(rows)
    if quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily benchmark session(s) for stock {}",
            quality["skipped_ambiguous_sessions"],
            stock_id,
        )
    return normalized


def _enhanced_scan_rows(db, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Load the daily feature inputs for the enhanced scanner in one query."""
    return db.fetch_all(
        f"""
        WITH universe AS (
            SELECT id, symbol, name, exchange, market_cap_cr, industry,
                   COUNT(*) OVER () AS universe_count
            FROM stocks
            WHERE {equity_universe_sql('stocks')}
        ), {_CANONICAL_DAILY_SESSION_CTES}, base AS (
            SELECT
                daily.stock_id,
                daily.timestamp,
                daily.session_date,
                daily.open,
                daily.high,
                daily.low,
                daily.close,
                daily.volume,
                daily.delivery_pct,
                LAG(daily.close) OVER (
                    PARTITION BY daily.stock_id ORDER BY daily.session_date ASC
                ) AS previous_close
            FROM daily_sessions daily
        ), ranked AS (
            SELECT
                b.*,
                ROW_NUMBER() OVER (
                    PARTITION BY b.stock_id ORDER BY b.session_date DESC
                ) AS rn
            FROM base b
        ), summary AS MATERIALIZED (
            SELECT
                stock_id,
                MAX(timestamp) FILTER (WHERE rn = 1) AS latest_timestamp,
                MAX(open) FILTER (WHERE rn = 1) AS latest_open,
                MAX(high) FILTER (WHERE rn = 1) AS latest_high,
                MAX(low) FILTER (WHERE rn = 1) AS latest_low,
                MAX(close) FILTER (WHERE rn = 1) AS latest_close,
                MAX(volume) FILTER (WHERE rn = 1) AS latest_volume,
                MAX(delivery_pct) FILTER (WHERE rn = 1) AS latest_delivery_pct,
                MAX(close) FILTER (WHERE rn = 11) AS close_10d,
                MAX(close) FILTER (WHERE rn = 21) AS close_20d,
                MAX(close) FILTER (WHERE rn = 61) AS close_60d,
                MAX(close) FILTER (WHERE rn = 121) AS close_120d,
                MAX(close) FILTER (WHERE rn = 253) AS close_252d,
                AVG(close) FILTER (WHERE rn BETWEEN 1 AND 20) AS sma_20,
                AVG(close) FILTER (WHERE rn BETWEEN 1 AND 50) AS sma_50,
                AVG(close) FILTER (WHERE rn BETWEEN 1 AND 200) AS sma_200,
                AVG(volume) FILTER (WHERE rn BETWEEN 2 AND 21) AS prior_20d_volume,
                AVG(volume) FILTER (WHERE rn BETWEEN 2 AND 61) AS prior_60d_volume,
                AVG(volume) FILTER (WHERE rn BETWEEN 2 AND 121) AS prior_120d_volume,
                AVG(volume) FILTER (WHERE rn BETWEEN 2 AND 253) AS prior_252d_volume,
                AVG(close * volume) FILTER (WHERE rn BETWEEN 1 AND 20) / 10000000.0 AS avg_turnover_20d_cr,
                AVG(close * volume) FILTER (WHERE rn BETWEEN 1 AND 60) / 10000000.0 AS avg_turnover_60d_cr,
                AVG(close * volume) FILTER (WHERE rn BETWEEN 1 AND 120) / 10000000.0 AS avg_turnover_120d_cr,
                AVG(close * volume) FILTER (WHERE rn BETWEEN 1 AND 252) / 10000000.0 AS avg_turnover_252d_cr,
                AVG(delivery_pct) FILTER (WHERE rn BETWEEN 1 AND 20) AS avg_delivery_pct_20d,
                AVG(delivery_pct) FILTER (WHERE rn BETWEEN 1 AND 60) AS avg_delivery_pct_60d,
                AVG(delivery_pct) FILTER (WHERE rn BETWEEN 1 AND 120) AS avg_delivery_pct_120d,
                AVG(delivery_pct) FILTER (WHERE rn BETWEEN 1 AND 252) AS avg_delivery_pct_252d,
                MAX(high) FILTER (WHERE rn BETWEEN 1 AND 252) AS high_52w,
                MAX(high) FILTER (WHERE rn BETWEEN 2 AND 21) AS prior_high_20,
                MAX(high) FILTER (WHERE rn BETWEEN 2 AND 56) AS prior_high_55,
                AVG((high - low) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 20) AS avg_range_pct_20d,
                AVG((high - low) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 60) AS avg_range_pct_60d,
                AVG((high - low) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 120) AS avg_range_pct_120d,
                AVG((high - low) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 252) AS avg_range_pct_252d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close))) FILTER (WHERE rn BETWEEN 1 AND 20) AS atr_abs_20d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close))) FILTER (WHERE rn BETWEEN 1 AND 60) AS atr_abs_60d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close))) FILTER (WHERE rn BETWEEN 1 AND 120) AS atr_abs_120d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close))) FILTER (WHERE rn BETWEEN 1 AND 252) AS atr_abs_252d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close)) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 20) AS atr_pct_20d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close)) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 60) AS atr_pct_60d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close)) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 120) AS atr_pct_120d,
                AVG(GREATEST(high - low, ABS(high - previous_close), ABS(low - previous_close)) / NULLIF(close, 0) * 100) FILTER (WHERE rn BETWEEN 1 AND 252) AS atr_pct_252d,
                STDDEV_SAMP(LN(close / NULLIF(previous_close, 0))) FILTER (WHERE rn BETWEEN 1 AND 20) * SQRT(252) * 100 AS realized_volatility_pct,
                STDDEV_SAMP(LN(close / NULLIF(previous_close, 0))) FILTER (WHERE rn BETWEEN 1 AND 60) * SQRT(252) * 100 AS realized_volatility_60d,
                STDDEV_SAMP(LN(close / NULLIF(previous_close, 0))) FILTER (WHERE rn BETWEEN 1 AND 120) * SQRT(252) * 100 AS realized_volatility_120d,
                STDDEV_SAMP(LN(close / NULLIF(previous_close, 0))) FILTER (WHERE rn BETWEEN 1 AND 252) * SQRT(252) * 100 AS realized_volatility_252d,
                AVG(CASE WHEN close > previous_close THEN 1.0 ELSE 0.0 END) FILTER (WHERE rn BETWEEN 1 AND 20) AS positive_ratio_20d,
                AVG(CASE WHEN close > previous_close THEN 1.0 ELSE 0.0 END) FILTER (WHERE rn BETWEEN 1 AND 60) AS positive_ratio_60d,
                AVG(CASE WHEN close > previous_close THEN 1.0 ELSE 0.0 END) FILTER (WHERE rn BETWEEN 1 AND 120) AS positive_ratio_120d,
                AVG(CASE WHEN close > previous_close THEN 1.0 ELSE 0.0 END) FILTER (WHERE rn BETWEEN 1 AND 252) AS positive_ratio_252d,
                COUNT(*) AS observations
            FROM ranked
            GROUP BY stock_id
        )
        SELECT
            u.id AS stock_id,
            u.symbol,
            u.name,
            u.industry,
            u.market_cap_cr,
            u.universe_count,
            s.*,
            CASE
                WHEN COALESCE(s.prior_20d_volume, 0) > 0
                THEN s.latest_volume / s.prior_20d_volume
                ELSE 0
            END AS volume_ratio,
            CASE
                WHEN COALESCE(s.prior_60d_volume, 0) > 0
                THEN s.latest_volume / s.prior_60d_volume
                ELSE 0
            END AS volume_ratio_60d,
            CASE
                WHEN COALESCE(s.prior_120d_volume, 0) > 0
                THEN s.latest_volume / s.prior_120d_volume
                ELSE 0
            END AS volume_ratio_120d,
            CASE
                WHEN COALESCE(s.prior_252d_volume, 0) > 0
                THEN s.latest_volume / s.prior_252d_volume
                ELSE 0
            END AS volume_ratio_252d,
            CASE
                WHEN COALESCE(s.latest_high - s.latest_low, 0) > 0
                THEN (s.latest_close - s.latest_low) / (s.latest_high - s.latest_low)
                ELSE 0.5
            END AS close_position
        FROM summary s
        JOIN universe u ON u.id = s.stock_id
        WHERE s.latest_close IS NOT NULL
        """,
        (start, end),
    )


def _enhanced_all_time_high_flags(db, stock_ids: list[int], end: datetime) -> dict[int, bool]:
    """Compare the latest visible daily high with its full stored history."""
    unique_ids = list(dict.fromkeys(stock_ids))
    if not unique_ids:
        return {}
    rows = db.fetch_all(
        f"""
        WITH universe AS (
            SELECT id FROM stocks
            WHERE id = ANY(%s) AND {equity_universe_sql('stocks')}
        ), {_CANONICAL_DAILY_SESSION_CTES}, selected_history AS MATERIALIZED (
            SELECT stock_id, timestamp, high
            FROM daily_sessions
        ), record_highs AS (
            SELECT stock_id, MAX(high) AS all_time_high
            FROM selected_history
            GROUP BY stock_id
        ), latest_highs AS (
            SELECT DISTINCT ON (stock_id) stock_id, high AS latest_high
            FROM selected_history
            ORDER BY stock_id, timestamp DESC
        )
        SELECT latest_highs.stock_id,
               latest_highs.latest_high >= record_highs.all_time_high AS at_all_time_high
        FROM latest_highs
        JOIN record_highs USING (stock_id)
        """,
        (
            unique_ids,
            datetime(1900, 1, 1, tzinfo=IST),
            datetime.combine(
                as_ist_datetime(end, date_time=MARKET_CLOSE).date(),
                time.min,
                tzinfo=IST,
            ),
        ),
    )
    return {
        int(row["stock_id"]): bool(row["at_all_time_high"])
        for row in rows
    }


@app.post("/api/enhanced-momentum-scanner/ath-markers")
def get_enhanced_ath_markers(request: EnhancedAthMarkersRequest):
    """Resolve ATH flags for cached scanner rows without rerunning the scan."""
    as_of = request.as_of_date
    end_exclusive = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    markers = _enhanced_all_time_high_flags(get_db(), request.stock_ids, end_exclusive)
    return {"markers": {str(stock_id): marker for stock_id, marker in markers.items()}}


def _screener_query_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return _serialize_row(dict(job))


def _screener_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("−", "-").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_screener_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        if not symbol or not name or len(symbol) > 40 or len(name) > 200:
            continue
        if not re.fullmatch(r"[A-Z0-9&._-]+", symbol) or symbol in seen:
            continue
        seen.add(symbol)
        industry = str(row.get("industry") or "").strip()[:160] or None
        clean.append({
            "symbol": symbol,
            "name": name,
            "industry": industry,
            "close": _screener_number(row.get("close")),
            "marketCapCr": _screener_number(row.get("marketCapCr")),
            "roePct": _screener_number(row.get("roePct")),
            "profitGrowth1yPct": _screener_number(row.get("profitGrowth1yPct")),
            "rank": len(clean) + 1,
        })
    return clean


def _expire_stale_screener_query_jobs(now: datetime | None = None) -> None:
    current_time = now or datetime.now(timezone.utc)
    for job in _screener_query_jobs.values():
        if job["status"] not in {"queued", "running", "waiting_for_login"}:
            continue
        created_at = job.get("started_at") or job["created_at"]
        if current_time - created_at < _SCREENER_QUERY_JOB_TIMEOUT:
            continue
        detail = "Screener refresh expired after 15 minutes. Start a new refresh."
        job.update({
            "status": "failed",
            "completed_at": current_time,
            "error": detail,
            "progress": {
                **job.get("progress", {}),
                "phase": "Failed",
                "detail": detail,
            },
        })


@app.post("/api/master/screener-query-candidates")
def start_screener_query_candidate_refresh(request: ScreenerQueryRefreshRequest):
    """Create a refresh job for the connector running in regular Chrome."""
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Enter the Screener query before refreshing candidates.")
    with _screener_query_lock:
        _expire_stale_screener_query_jobs()
        active = next(
            (job for job in _screener_query_jobs.values() if job["status"] in {"queued", "running", "waiting_for_login"}),
            None,
        )
        if active is not None:
            raise HTTPException(status_code=409, detail="A Screener query refresh is already running.")
        while len(_screener_query_jobs) >= _MAX_SCREENER_QUERY_JOBS:
            terminal_ids = [
                known_id for known_id, job in _screener_query_jobs.items()
                if job["status"] not in {"queued", "running", "waiting_for_login"}
            ]
            if not terminal_ids:
                break
            oldest_id = min(terminal_ids, key=lambda known_id: _screener_query_jobs[known_id]["created_at"])
            del _screener_query_jobs[oldest_id]
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "query": query,
            "status": "queued",
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
            "updated_at": None,
            "progress": {
                "phase": "Queued",
                "pages_completed": 0,
                "candidates_found": 0,
                "detail": "Waiting for the browser worker",
            },
            "result": None,
            "error": None,
        }
        _screener_query_jobs[job_id] = job
        response = _screener_query_job_snapshot(job)

    return response


@app.get("/api/master/screener-query-candidates/{job_id}")
def get_screener_query_candidate_refresh(job_id: str):
    """Return progress and, on completion, every collected screen row."""
    with _screener_query_lock:
        _expire_stale_screener_query_jobs()
        job = _screener_query_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Screener query refresh was not found or has expired.")
        return _screener_query_job_snapshot(job)


@app.post("/api/master/screener-query-candidates/{job_id}/progress")
def report_screener_query_candidate_progress(job_id: str, request: ScreenerQueryRefreshProgressRequest):
    """Accept progress from the local Chrome connector."""
    now = datetime.now(timezone.utc)
    with _screener_query_lock:
        _expire_stale_screener_query_jobs(now)
        job = _screener_query_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Active Screener query refresh was not found.")
        if job["status"] not in {"queued", "running", "waiting_for_login"}:
            return _screener_query_job_snapshot(job)
        job.update({
            "status": request.status,
            "started_at": job.get("started_at") or now,
            "progress": {
                "phase": request.phase,
                "pages_completed": request.pages_completed,
                "candidates_found": request.candidates_found,
                "detail": request.detail,
            },
        })
        return _screener_query_job_snapshot(job)


@app.post("/api/master/screener-query-candidates/{job_id}/complete")
def complete_screener_query_candidate_refresh(job_id: str, request: ScreenerQueryRefreshCompleteRequest):
    """Store the result set collected by the local Chrome connector."""
    rows = _clean_screener_rows(request.rows)
    completed_at = datetime.now(timezone.utc)
    with _screener_query_lock:
        _expire_stale_screener_query_jobs(completed_at)
        job = _screener_query_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Active Screener query refresh was not found.")
        if job["status"] == "completed":
            return _screener_query_job_snapshot(job)
        if job["status"] == "failed":
            return _screener_query_job_snapshot(job)
        job.update({
            "status": "completed",
            "started_at": job.get("started_at") or completed_at,
            "completed_at": completed_at,
            "updated_at": completed_at,
            "result": {
                "query": job["query"],
                "page_count": request.page_count,
                "result_count": len(rows),
                "rows": rows,
                "updated_at": completed_at,
            },
            "error": None,
            "progress": {
                "phase": "Complete",
                "pages_completed": request.page_count,
                "candidates_found": len(rows),
                "detail": f"Collected {len(rows)} companies from {request.page_count} pages.",
            },
        })
        return _screener_query_job_snapshot(job)


@app.post("/api/master/screener-query-candidates/{job_id}/fail")
def fail_screener_query_candidate_refresh(job_id: str, request: ScreenerQueryRefreshFailRequest):
    """Record a connector error so the UI can offer a useful retry."""
    failed_at = datetime.now(timezone.utc)
    with _screener_query_lock:
        _expire_stale_screener_query_jobs(failed_at)
        job = _screener_query_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Active Screener query refresh was not found.")
        if job["status"] in {"completed", "failed"}:
            return _screener_query_job_snapshot(job)
        detail = re.sub(r"\s+", " ", request.error).strip()[:500] or "Screener refresh failed."
        job.update({
            "status": "failed",
            "started_at": job.get("started_at") or failed_at,
            "completed_at": failed_at,
            "error": detail,
            "progress": {
                **job.get("progress", {}),
                "phase": "Failed",
                "detail": detail,
            },
        })
        logger.bind(screener_query_refresh=True).warning(
            "refresh_failed job_id={} detail={}", job_id, detail,
        )
        return _screener_query_job_snapshot(job)


@app.post("/api/enhanced-momentum-scanner/scan")
def scan_enhanced_momentum_universe(request: EnhancedMomentumScanRequest):
    return _scan_enhanced_momentum_universe(request, include_details=True)


@app.post("/api/enhanced-momentum-scanner/setup-candles")
def get_enhanced_setup_candles(request: EnhancedSetupCandlesRequest):
    """Return recent daily candles in one request for candidate setup detection."""
    as_of = _last_completed_daily_scan_date(request.as_of_date)
    end_exclusive = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    start_inclusive = end_exclusive - timedelta(days=request.days)
    rows = get_db().fetch_all(
        f"""
        SELECT c.stock_id, c.timestamp, c.open, c.high, c.low, c.close, c.volume
        FROM candles_1d c
        JOIN stocks s ON s.id = c.stock_id
        WHERE c.stock_id = ANY(%s)
          AND {equity_universe_sql('s')}
          AND c.timestamp >= %s AND c.timestamp < %s
        ORDER BY stock_id, timestamp ASC
        """,
        (list(dict.fromkeys(request.stock_ids)), start_inclusive, end_exclusive),
    )
    rows, daily_quality = normalize_daily_candles(rows)
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily session(s) while loading setup candles",
            daily_quality["skipped_ambiguous_sessions"],
        )
    candles_by_stock: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        stock_id = str(int(row["stock_id"]))
        candles_by_stock.setdefault(stock_id, []).append({
            "time": int(row["timestamp"].timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row["volume"]),
        })
    return {"candles_by_stock": candles_by_stock}


def _scan_enhanced_momentum_universe(
    request: EnhancedMomentumScanRequest,
    *,
    include_details: bool,
    include_all_results: bool = False,
):
    """Rank daily NSE candidates with market-aware swing-momentum features."""
    db = get_db()
    as_of = _last_completed_daily_scan_date(request.as_of_date or datetime.now(IST).date())
    # Candle timestamps represent NSE sessions in Asia/Kolkata. A UTC
    # midnight boundary includes the first 5.5 hours of the next Indian date,
    # leaking tomorrow's daily candle into historical scans.
    end_exclusive = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    # Load only enough calendar time for the requested session history and
    # the scanner's longest 252-session feature, with a 10% allowance for
    # holidays and missing candles. The feature query canonicalizes duplicate
    # rows into exchange sessions after loading.
    history_days = _enhanced_scan_history_days(request.min_history)
    start_inclusive = end_exclusive - timedelta(days=history_days)

    rows = _enhanced_scan_rows(db, start_inclusive, end_exclusive)
    universe_count = int(_number(rows[0].get("universe_count"))) if rows else 0
    benchmark_rows = _enhanced_benchmark_candidates(db)
    nifty_stock, sensex_stock = benchmark_rows
    nifty_candles = (
        _load_enhanced_daily_candles(db, int(nifty_stock["id"]), start_inclusive, end_exclusive)
        if nifty_stock
        else []
    )
    sensex_candles = (
        _load_enhanced_daily_candles(db, int(sensex_stock["id"]), start_inclusive, end_exclusive)
        if sensex_stock
        else []
    )
    market_context = build_market_context(nifty_candles, sensex_candles)
    market_context["sector"] = {
        "available": False,
        "weight": 0.0,
        "status": "reserved_for_future_point_in_time_sector_data",
    }

    data_rows: list[dict[str, Any]] = []
    for source in rows:
        if _number(source.get("observations")) < request.min_history:
            continue
        row = dict(source)
        latest_close = _number(row.get("latest_close"))
        for field_name, close_field in (
            ("return_10d", "close_10d"),
            ("return_20d", "close_20d"),
            ("return_60d", "close_60d"),
            ("return_120d", "close_120d"),
            ("return_252d", "close_252d"),
        ):
            prior_close = _number(row.get(close_field))
            row[field_name] = (
                (latest_close / prior_close - 1.0) * 100.0
                if latest_close and prior_close
                else None
            )
        data_rows.append(row)
    enriched = enrich_candidates(
        data_rows,
        market_context,
        min_history=request.min_history,
    )

    scored: list[dict[str, Any]] = []
    for item in enriched:
        latest_close = _number(item.get("latest_close")) or 0.0
        return_20 = _number(item.get("return_20d"))
        return_60 = _number(item.get("return_60d"))
        trend_aligned = (
            (_number(item.get("sma_20")) or 0) > (_number(item.get("sma_50")) or 0) > 0
        )
        regime = str(market_context.get("regime") or "unavailable")
        if request.market_mode == "bullish_only" and regime != "bullish":
            continue
        if request.market_mode == "bullish_or_neutral" and regime in {"bearish", "unavailable"}:
            continue
        if latest_close < request.min_price:
            continue
        if (_number(item.get("avg_turnover_20d_cr")) or 0) < request.min_avg_turnover_cr:
            continue
        if (_number(item.get("volume_ratio")) or 0) < request.min_volume_ratio:
            continue
        if return_20 is None or return_20 < request.min_return_20d:
            continue
        if return_60 is None or return_60 < request.min_return_60d:
            continue
        if request.trend_mode == "aligned" and not trend_aligned:
            continue
        if request.trend_mode == "above_sma50" and latest_close <= (_number(item.get("sma_50")) or 0):
            continue
        if request.require_breakout and not (item.get("breakout_20") or item.get("breakout_55")):
            continue
        if (_number(item.get("enhanced_score")) or 0) < request.min_score:
            continue

        latest_timestamp = item.get("latest_timestamp")
        result = {
            "stock_id": item["stock_id"],
            "symbol": item["symbol"],
            "name": item["name"],
            "exchange": item.get("exchange"),
            "company_name": _stock_company_name(item["symbol"], item["name"]) or "",
            "industry": item.get("industry") or "",
            "market_cap_cr": round(_number(item.get("market_cap_cr")) or 0.0, 2),
            "as_of_date": latest_timestamp.date().isoformat() if hasattr(latest_timestamp, "date") else as_of.isoformat(),
            "close": round(latest_close, 2),
            "return_10d": round(_number(item.get("return_10d")) or 0.0, 2) if item.get("return_10d") is not None else None,
            "return_20d": round(return_20, 2) if return_20 is not None else None,
            "return_60d": round(return_60, 2) if return_60 is not None else None,
            "return_120d": round(_number(item.get("return_120d")) or 0.0, 2) if item.get("return_120d") is not None else None,
            "return_252d": round(_number(item.get("return_252d")) or 0.0, 2) if item.get("return_252d") is not None else None,
            "market_relative_20d": round(_number(item.get("market_relative_20d")) or 0.0, 2) if item.get("market_relative_20d") is not None else None,
            "market_relative_60d": round(_number(item.get("market_relative_60d")) or 0.0, 2) if item.get("market_relative_60d") is not None else None,
            "market_relative_120d": round(_number(item.get("market_relative_120d")) or 0.0, 2) if item.get("market_relative_120d") is not None else None,
            "volume_ratio": round(_number(item.get("volume_ratio")) or 0.0, 2),
            "volume_ratio_20d": round(_number(item.get("volume_ratio")) or 0.0, 2),
            "volume_ratio_60d": round(_number(item.get("volume_ratio_60d")) or 0.0, 2),
            "volume_ratio_120d": round(_number(item.get("volume_ratio_120d")) or 0.0, 2),
            "volume_ratio_252d": round(_number(item.get("volume_ratio_252d")) or 0.0, 2),
            "avg_turnover_20d_cr": round(_number(item.get("avg_turnover_20d_cr")) or 0.0, 2),
            "avg_turnover_60d_cr": round(_number(item.get("avg_turnover_60d_cr")) or 0.0, 2),
            "avg_turnover_120d_cr": round(_number(item.get("avg_turnover_120d_cr")) or 0.0, 2),
            "avg_turnover_252d_cr": round(_number(item.get("avg_turnover_252d_cr")) or 0.0, 2),
            "avg_delivery_pct_20d": round(_number(item.get("avg_delivery_pct_20d")) or 0.0, 2),
            "avg_delivery_pct_60d": round(_number(item.get("avg_delivery_pct_60d")) or 0.0, 2),
            "avg_delivery_pct_120d": round(_number(item.get("avg_delivery_pct_120d")) or 0.0, 2),
            "avg_delivery_pct_252d": round(_number(item.get("avg_delivery_pct_252d")) or 0.0, 2),
            "atr_pct_20d": round(_number(item.get("atr_pct_20d")) or 0.0, 2),
            "atr_pct_60d": round(_number(item.get("atr_pct_60d")) or 0.0, 2),
            "atr_pct_120d": round(_number(item.get("atr_pct_120d")) or 0.0, 2),
            "atr_pct_252d": round(_number(item.get("atr_pct_252d")) or 0.0, 2),
            "realized_volatility_pct": round(_number(item.get("realized_volatility_pct")) or 0.0, 2),
            "realized_volatility_20d": round(_number(item.get("realized_volatility_pct")) or 0.0, 2),
            "realized_volatility_60d": round(_number(item.get("realized_volatility_60d")) or 0.0, 2),
            "realized_volatility_120d": round(_number(item.get("realized_volatility_120d")) or 0.0, 2),
            "realized_volatility_252d": round(_number(item.get("realized_volatility_252d")) or 0.0, 2),
            "positive_ratio_20d": round((_number(item.get("positive_ratio_20d")) or 0.0) * 100, 2),
            "positive_ratio_60d": round((_number(item.get("positive_ratio_60d")) or 0.0) * 100, 2),
            "positive_ratio_120d": round((_number(item.get("positive_ratio_120d")) or 0.0) * 100, 2),
            "positive_ratio_252d": round((_number(item.get("positive_ratio_252d")) or 0.0) * 100, 2),
            "breakout_20": bool(item.get("breakout_20")),
            "breakout_55": bool(item.get("breakout_55")),
            "breakout_margin_atr": round(_number(item.get("breakout_margin_atr")) or 0.0, 2) if item.get("breakout_margin_atr") is not None else None,
            "extension_atr": round(_number(item.get("extension_atr")) or 0.0, 2) if item.get("extension_atr") is not None else None,
            "observations": int(_number(item.get("observations")) or 0),
            "enhanced_score": round(_number(item.get("enhanced_score")) or 0.0, 2),
            "base_momentum_score": item.get("base_momentum_score"),
            "market_relative_strength_score": item.get("market_relative_strength_score"),
            "risk_adjusted_momentum_score": item.get("risk_adjusted_momentum_score"),
            "risk_adjusted_20d": item.get("risk_adjusted_20d"),
            "risk_adjusted_60d": item.get("risk_adjusted_60d"),
            "risk_adjusted_120d": item.get("risk_adjusted_120d"),
            "risk_adjusted_252d": item.get("risk_adjusted_252d"),
            "volume_participation_score": item.get("volume_participation_score"),
            "volume_ratio_score": item.get("volume_ratio_score"),
            "turnover_score": item.get("turnover_score"),
            "breakout_quality_score": item.get("breakout_quality_score"),
            "market_regime_score": item.get("market_regime_score"),
            "sector_score": None,
            "sector_context_available": False,
            "trend_aligned": trend_aligned,
            "category": item.get("category", "Enhanced momentum"),
        }
        scored.append(result)

    scored.sort(key=lambda item: (-item["enhanced_score"], -item["market_relative_60d"] if item["market_relative_60d"] is not None else 0, item["symbol"]))
    # Historical replay workers first fetch all matches, then remove stale
    # snapshots before applying Top N so stale rows cannot consume the user's
    # requested cohort size. Live scans apply the same optional rank filter
    # after all candidate scores have been calculated.
    visible_results = (
        scored
        if include_all_results or request.top_n is None
        else scored[: request.top_n]
    )
    ath_markers = (
        _enhanced_all_time_high_flags(
            db,
            [int(item["stock_id"]) for item in visible_results],
            end_exclusive,
        )
        if include_details
        else {}
    )
    for rank, item in enumerate(visible_results, start=1):
        item["rank"] = rank
        item["at_all_time_high"] = ath_markers.get(int(item["stock_id"]), False)

    results = _attach_stock_sectors(db, visible_results) if include_details else visible_results

    return {
        "scanner": "enhanced_swing_momentum",
        "as_of_date": as_of.isoformat(),
        "universe": "NSE and BSE company equities (ETFs and index instruments excluded)",
        "universe_stocks": universe_count,
        "scanned_stocks": len(rows),
        "history_eligible_stocks": len(data_rows),
        "matching_stocks": len(scored),
        "returned_stocks": len(results),
        "filters": request.model_dump(mode="json"),
        "market_context": market_context,
        "formula": {
            "score": "45% base momentum + 25% market-relative strength + 20% volatility-adjusted momentum + 10% breakout quality",
            "base_momentum": "Cross-sectional percentiles of 20d, 60d, 120d and 252d returns",
            "market_relative_strength": "Stock return less the primary benchmark return across 20d, 60d and 120d",
            "risk_adjusted_momentum": "20d, 60d, 120d and 252d returns divided by matching-horizon realised volatility or ATR percentage",
            "breakout_quality": "Prior 20/55-session high proximity, multi-horizon volume participation, close location and ATR extension penalty",
            "volatility": "ATR and annualised realised volatility across 20d, 60d, 120d and 252d horizons",
            "volume_liquidity": "Volume ratios and traded value across 20d, 60d, 120d and 252d horizons",
            "market_context": "Market regime is reported separately and does not contribute to the score; Nifty remains the relative-strength benchmark",
            "instrument_universe": "Active NSE and BSE company equities; ETFs and index instruments are excluded from the stock universe.",
        },
        "results": results,
    }


@app.post("/api/master/candidate-metrics")
def get_master_candidate_metrics(request: MasterCandidateMetricsRequest):
    """Fill price, volume and momentum fields for imported Master candidates."""
    stock_ids = list(dict.fromkeys(stock_id for stock_id in request.stock_ids if stock_id > 0))
    if not stock_ids:
        return {"metrics_by_stock_id": {}}

    scan_request = EnhancedMomentumScanRequest(
        as_of_date=request.as_of_date,
        top_n=None,
        min_price=0,
        min_avg_turnover_cr=0,
        min_score=0,
        min_volume_ratio=0,
        min_return_20d=-100,
        min_return_60d=-100,
        min_history=60,
        trend_mode="any",
        market_mode="all",
        require_breakout=False,
    )
    scan = _scan_enhanced_momentum_universe(
        scan_request,
        include_details=False,
        include_all_results=True,
    )
    requested_ids = set(stock_ids)
    metric_fields = (
        "return_20d", "return_60d", "return_120d", "return_252d",
        "market_relative_20d", "market_relative_60d", "market_relative_120d",
        "volume_ratio", "volume_ratio_20d", "volume_ratio_60d", "volume_ratio_120d", "volume_ratio_252d",
        "atr_pct_20d", "atr_pct_60d", "atr_pct_120d", "atr_pct_252d",
        "realized_volatility_pct", "realized_volatility_20d", "realized_volatility_60d", "realized_volatility_120d", "realized_volatility_252d",
        "breakout_20", "breakout_55", "enhanced_score", "base_momentum_score",
        "market_relative_strength_score", "risk_adjusted_momentum_score", "risk_adjusted_20d",
        "risk_adjusted_60d", "risk_adjusted_120d", "risk_adjusted_252d",
        "volume_participation_score", "volume_ratio_score", "turnover_score", "breakout_quality_score",
        "trend_aligned", "observations", "category",
    )
    metrics_by_stock_id = {
        str(int(item["stock_id"])): {field: item.get(field) for field in metric_fields}
        for item in scan["results"]
        if int(item["stock_id"]) in requested_ids
    }

    as_of = _last_completed_daily_scan_date(request.as_of_date)
    end_exclusive = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    ath_markers = _enhanced_all_time_high_flags(get_db(), stock_ids, end_exclusive)
    for stock_id, marker in ath_markers.items():
        if str(stock_id) in metrics_by_stock_id:
            metrics_by_stock_id[str(stock_id)]["at_all_time_high"] = marker

    return {"metrics_by_stock_id": metrics_by_stock_id}


def _enhanced_momentum_scan_session_worker(
    session_date: date,
    filter_values: dict[str, Any],
) -> dict[str, Any]:
    """Compute all point-in-time scanner matches in a spawned DB worker."""
    snapshot_request = EnhancedMomentumScanRequest(**filter_values, as_of_date=session_date)
    return _scan_enhanced_momentum_universe(
        snapshot_request,
        include_details=False,
        include_all_results=True,
    )


def _add_calendar_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _enhanced_backtest_window(request: EnhancedMomentumBacktestRequest) -> tuple[date, date, int]:
    end_date = request.end_date or datetime.now(IST).date()
    start_date = request.start_date or _add_calendar_months(end_date, -request.lookback_months)
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="From date must be on or before To date")
    stored_months = max(1, (end_date.year - start_date.year) * 12 + end_date.month - start_date.month)
    return start_date, end_date, stored_months


def _enhanced_backtest_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        local = as_ist_datetime(value)
        return local.date() if local is not None else None
    if isinstance(value, date):
        return value
    if value is None:
        return None
    local = as_ist_datetime(value, date_time=MARKET_CLOSE)
    return local.date() if local is not None else None


def _enhanced_backtest_timestamp(value: Any, *, date_time: time) -> datetime | None:
    """Normalize date-only daily bars and intraday filter trades for portfolio ordering."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, date_time, tzinfo=IST)
    elif value is None:
        return None
    else:
        raw = str(value)
        if len(raw) <= 10:
            session_date = _enhanced_backtest_date(raw)
            return datetime.combine(session_date, date_time, tzinfo=IST) if session_date is not None else None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            session_date = _enhanced_backtest_date(raw)
            if session_date is None:
                return None
            parsed = datetime.combine(session_date, date_time, tzinfo=IST)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _enhanced_momentum_refresh_dates(
    session_dates: list[date],
    start_date: date,
    end_date: date,
    frequency: str,
) -> list[date]:
    """Map calendar refresh targets to the first available NSE session on or after each target."""
    available_sessions = [session for session in session_dates if start_date <= session <= end_date]
    if not available_sessions:
        return []
    step_months = {"monthly": 1, "quarterly": 3, "semiannual": 6}.get(frequency)
    targets: list[date] = []
    sequence = 0
    while True:
        if frequency == "15_days":
            target = start_date + timedelta(days=15 * sequence)
        elif step_months is not None:
            target = _add_calendar_months(start_date, step_months * sequence)
        else:
            raise ValueError(f"Unsupported Enhanced Momentum refresh frequency: {frequency}")
        if target > end_date:
            break
        targets.append(target)
        sequence += 1
    if not targets:
        targets = [start_date]

    scheduled: list[date] = []
    session_index = 0
    for target in targets:
        while session_index < len(available_sessions) and available_sessions[session_index] < target:
            session_index += 1
        if session_index >= len(available_sessions):
            break
        session = available_sessions[session_index]
        if not scheduled or session > scheduled[-1]:
            scheduled.append(session)
    return scheduled


def _enhanced_momentum_refresh_label(frequency: str) -> str:
    return {
        "15_days": "every 15 days",
        "monthly": "monthly",
        "quarterly": "quarterly",
        "semiannual": "semi-annually",
    }.get(frequency, frequency)


def _enhanced_backtest_trade(
    candidate: dict[str, Any],
    candles: list[dict[str, Any]],
    end_date: date,
    target_atr: float,
    stop_atr: float,
    capital_per_stock: float,
    charge_config: DeliveryChargeConfig,
) -> dict[str, Any]:
    """Simulate one point-in-time scanner signal through its forward window."""
    signal_date = _enhanced_backtest_date(candidate.get("as_of_date"))
    close = _number(candidate.get("close"))
    if signal_date is None or close is None or close <= 0:
        return {**candidate, "entry_date": None, "entry_price": None, "outcome": "invalid_signal_data"}

    future = [
        row for row in candles
        if (candle_date := _enhanced_backtest_date(row.get("timestamp"))) is not None
        and signal_date < candle_date <= end_date
        and (_number(row.get("open")) or 0) > 0
        and (_number(row.get("close")) or 0) > 0
    ]
    future.sort(key=lambda row: _enhanced_backtest_date(row.get("timestamp")) or date.max)
    if not future:
        return {
            **candidate,
            "signal_date": signal_date.isoformat(),
            "entry_date": None,
            "entry_price": None,
            "outcome": "no_forward_candles",
        }

    entry_candle = future[0]
    entry_date = _enhanced_backtest_date(entry_candle.get("timestamp"))
    entry_price = _number(entry_candle.get("open"))
    if entry_date is None or entry_price is None or entry_price <= 0:
        return {**candidate, "signal_date": signal_date.isoformat(), "entry_date": None, "entry_price": None, "outcome": "invalid_entry_candle"}

    atr_pct = _number(candidate.get("atr_pct_20d"))
    atr_abs = close * atr_pct / 100.0 if atr_pct is not None and atr_pct > 0 else None
    target_price = entry_price + target_atr * atr_abs if atr_abs is not None else None
    stop_price = entry_price - stop_atr * atr_abs if atr_abs is not None else None

    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason = "period_end"
    target_hit = False
    stop_hit = False
    if target_price is not None and stop_price is not None and stop_price > 0:
        for candle in future:
            candle_date = _enhanced_backtest_date(candle.get("timestamp"))
            candle_open = _number(candle.get("open"))
            candle_high = _number(candle.get("high"))
            candle_low = _number(candle.get("low"))
            if candle_date is None or candle_open is None or candle_high is None or candle_low is None:
                continue
            if candle_open >= target_price:
                exit_date, exit_price, exit_reason, target_hit = candle_date, candle_open, "target_gap", True
                break
            if candle_open <= stop_price:
                exit_date, exit_price, exit_reason, stop_hit = candle_date, candle_open, "stop_gap", True
                break
            hit_target = candle_high >= target_price
            hit_stop = candle_low <= stop_price
            if hit_target and hit_stop:
                # Daily OHLC bars cannot tell which level was touched first;
                # assume the adverse stop was reached first.
                exit_date, exit_price, exit_reason, stop_hit = candle_date, stop_price, "stop_same_candle", True
                break
            if hit_stop:
                exit_date, exit_price, exit_reason, stop_hit = candle_date, stop_price, "stop_loss", True
                break
            if hit_target:
                exit_date, exit_price, exit_reason, target_hit = candle_date, target_price, "target", True
                break

    horizon_candle = future[-1]
    horizon_date = _enhanced_backtest_date(horizon_candle.get("timestamp"))
    horizon_close = _number(horizon_candle.get("close"))
    if exit_price is None:
        exit_date = horizon_date
        exit_price = horizon_close
        exit_reason = "period_end" if target_price is not None else "period_end_atr_unavailable"

    horizon_return_pct = (horizon_close / entry_price - 1.0) * 100.0 if horizon_close is not None else None
    gross_trade_return_pct = (exit_price / entry_price - 1.0) * 100.0 if exit_price is not None else None
    quantity = charge_config.max_buy_quantity(capital_per_stock, entry_price)
    buy_charges = charge_config.calculate_order("BUY", entry_price, quantity).total if quantity else 0.0
    sell_charges = charge_config.calculate_order("SELL", exit_price, quantity).total if quantity and exit_price is not None else 0.0
    invested = quantity * entry_price + buy_charges
    net_pnl = quantity * (exit_price - entry_price) - buy_charges - sell_charges if quantity and exit_price is not None else None
    net_trade_return_pct = (net_pnl / invested * 100.0) if net_pnl is not None and invested > 0 else None
    horizon_complete = bool(horizon_date and (end_date - horizon_date).days <= 5)

    return {
        **candidate,
        "signal_date": signal_date.isoformat(),
        "entry_date": entry_date.isoformat(),
        "entry_price": round(entry_price, 4),
        "target_price": round(target_price, 4) if target_price is not None else None,
        "stop_price": round(stop_price, 4) if stop_price is not None else None,
        "target_hit": target_hit,
        "stop_hit": stop_hit,
        "exit_date": exit_date.isoformat() if exit_date else None,
        "exit_price": round(exit_price, 4) if exit_price is not None else None,
        "exit_reason": exit_reason,
        "horizon_end_date": horizon_date.isoformat() if horizon_date else None,
        "horizon_close": round(horizon_close, 4) if horizon_close is not None else None,
        "horizon_complete": horizon_complete,
        "horizon_return_pct": round(horizon_return_pct, 4) if horizon_return_pct is not None else None,
        "gross_trade_return_pct": round(gross_trade_return_pct, 4) if gross_trade_return_pct is not None else None,
        "net_trade_return_pct": round(net_trade_return_pct, 4) if net_trade_return_pct is not None else None,
        "quantity": quantity,
        "buy_charges": round(buy_charges, 2),
        "sell_charges": round(sell_charges, 2),
        "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
        "outcome": "capital_too_small" if quantity == 0 else "completed" if horizon_complete else "partial_forward_data",
    }


def _enhanced_backtest_replay_stock(
    candidates: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    target_atr: float,
    stop_atr: float,
    capital_per_stock: float,
    charge_config: DeliveryChargeConfig,
) -> dict[str, Any]:
    """Compound one stock's capital through every non-overlapping scanner signal."""
    candles, daily_quality = normalize_daily_candles(candles)
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily candle session(s) for {} during enhanced momentum replay",
            daily_quality["skipped_ambiguous_sessions"],
            candidates[-1].get("symbol", "unknown") if candidates else "unknown",
        )
    ordered = sorted(
        (
            dict(candle)
            for candle in candles
            if (candle_date := _enhanced_backtest_date(candle.get("timestamp"))) is not None
            and start_date <= candle_date <= end_date
        ),
        key=lambda candle: _enhanced_backtest_date(candle.get("timestamp")) or date.max,
    )
    signal_by_date = {
        signal_date.isoformat(): candidate
        for candidate in candidates
        if (signal_date := _enhanced_backtest_date(candidate.get("as_of_date"))) is not None
    }
    cash = float(capital_per_stock)
    position: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []

    def close_trade(exit_date: date, exit_price: float, reason: str, target_hit: bool, stop_hit: bool) -> None:
        nonlocal cash, position
        if position is None:
            return
        quantity = int(position["quantity"])
        sell_charges = charge_config.calculate_order("SELL", exit_price, quantity).total
        net_pnl = quantity * (exit_price - float(position["entry_price"])) - float(position["buy_charges"]) - sell_charges
        invested = quantity * float(position["entry_price"]) + float(position["buy_charges"])
        cash += quantity * exit_price - sell_charges
        trades.append({
            "stock_id": position["candidate"].get("stock_id"),
            "symbol": position["candidate"].get("symbol"),
            "name": position["candidate"].get("name") or "",
            "company_name": position["candidate"].get("company_name") or "",
            "rank": position["candidate"].get("rank"),
            "enhanced_score": position["candidate"].get("enhanced_score"),
            "signal_date": position["signal_date"],
            "entry_date": position["entry_date"],
            "entry_price": round(float(position["entry_price"]), 4),
            "target_price": round(float(position["target_price"]), 4) if position["target_price"] is not None else None,
            "stop_price": round(float(position["stop_price"]), 4) if position["stop_price"] is not None else None,
            "target_hit": target_hit,
            "stop_hit": stop_hit,
            "exit_date": exit_date.isoformat(),
            "exit_price": round(exit_price, 4),
            "exit_reason": reason,
            "quantity": quantity,
            "buy_charges": round(float(position["buy_charges"]), 2),
            "sell_charges": round(sell_charges, 2),
            "gross_pnl": round(quantity * (exit_price - float(position["entry_price"])), 2),
            "net_pnl": round(net_pnl, 2),
            "net_trade_return_pct": round(net_pnl / invested * 100.0, 4) if invested else None,
            "capital_after": round(cash, 2),
        })
        position = None

    previous_date: date | None = None
    last_candle: dict[str, Any] | None = None
    for candle in ordered:
        candle_date = _enhanced_backtest_date(candle.get("timestamp"))
        open_price = _number(candle.get("open"))
        high_price = _number(candle.get("high"))
        low_price = _number(candle.get("low"))
        if candle_date is None or open_price <= 0 or high_price <= 0 or low_price <= 0:
            previous_date = candle_date or previous_date
            continue
        last_candle = candle
        was_open_at_start = position is not None

        if position is not None:
            target = position["target_price"]
            stop = position["stop_price"]
            exit_data: tuple[float, str, bool, bool] | None = None
            if target is not None and open_price >= target:
                exit_data = (open_price, "target_gap", True, False)
            elif stop is not None and open_price <= stop:
                exit_data = (open_price, "stop_gap", False, True)
            elif target is not None and stop is not None and high_price >= target and low_price <= stop:
                # Daily OHLC cannot establish which threshold came first.
                exit_data = (float(stop), "stop_same_candle", False, True)
            elif stop is not None and low_price <= stop:
                exit_data = (float(stop), "stop_loss", False, True)
            elif target is not None and high_price >= target:
                exit_data = (float(target), "target", True, False)
            if exit_data is not None:
                close_trade(candle_date, *exit_data)

        if not was_open_at_start and position is None and previous_date is not None:
            candidate = signal_by_date.get(previous_date.isoformat())
            if candidate is not None:
                entry_price = open_price
                atr_pct = _number(candidate.get("atr_pct_20d"), -1)
                signal_close = _number(candidate.get("close"), -1)
                atr_abs = signal_close * atr_pct / 100.0 if signal_close > 0 and atr_pct > 0 else None
                target_price = entry_price + target_atr * atr_abs if atr_abs is not None else None
                stop_price = entry_price - stop_atr * atr_abs if atr_abs is not None else None
                if stop_price is not None and stop_price <= 0:
                    stop_price = None
                quantity = charge_config.max_buy_quantity(cash, entry_price)
                if quantity > 0:
                    buy_charges = charge_config.calculate_order("BUY", entry_price, quantity).total
                    cash -= quantity * entry_price + buy_charges
                    position = {
                        "candidate": candidate,
                        "signal_date": previous_date.isoformat(),
                        "entry_date": candle_date.isoformat(),
                        "entry_price": entry_price,
                        "target_price": target_price,
                        "stop_price": stop_price,
                        "quantity": quantity,
                        "buy_charges": buy_charges,
                    }
                    # An entry day's range can already touch either bracket.
                    if target_price is not None and stop_price is not None and high_price >= target_price and low_price <= stop_price:
                        close_trade(candle_date, stop_price, "stop_same_candle", False, True)
                    elif target_price is not None and open_price >= target_price:
                        close_trade(candle_date, open_price, "target_gap", True, False)
                    elif stop_price is not None and open_price <= stop_price:
                        close_trade(candle_date, open_price, "stop_gap", False, True)
                    elif stop_price is not None and low_price <= stop_price:
                        close_trade(candle_date, stop_price, "stop_loss", False, True)
                    elif target_price is not None and high_price >= target_price:
                        close_trade(candle_date, target_price, "target", True, False)

        previous_date = candle_date

    if position is not None and last_candle is not None:
        exit_date = _enhanced_backtest_date(last_candle.get("timestamp"))
        exit_price = _number(last_candle.get("close"), -1)
        if exit_date is not None and exit_price > 0:
            close_trade(exit_date, exit_price, "end_of_test", False, False)

    symbol_info = candidates[-1] if candidates else {}
    final_capital = cash
    net_pnl = final_capital - capital_per_stock
    trade_count = len(trades)
    target_hits = sum(bool(trade["target_hit"]) for trade in trades)
    stop_hits = sum(bool(trade["stop_hit"]) for trade in trades)
    wins = sum(float(trade.get("net_pnl") or 0) > 0 for trade in trades)
    return {
        "stock_id": symbol_info.get("stock_id"),
        "symbol": symbol_info.get("symbol"),
        "name": symbol_info.get("name") or "",
        "company_name": symbol_info.get("company_name") or "",
        "industry": symbol_info.get("industry") or "",
        "scanner_signal_count": len(candidates),
        "starting_capital": round(capital_per_stock, 2),
        "final_capital": round(final_capital, 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct": round(net_pnl / capital_per_stock * 100.0, 4) if capital_per_stock else None,
        "total_trades": trade_count,
        "winning_trades": wins,
        "win_rate_pct": round(wins / trade_count * 100.0, 2) if trade_count else None,
        "target_hits": target_hits,
        "stop_hits": stop_hits,
        "status": "completed" if trade_count else "no_trade",
        "trades": trades,
    }


def _enhanced_backtest_idle_stock(candidate: dict[str, Any], capital: float, status: str) -> dict[str, Any]:
    """Keep every scanner-eligible stock visible, including unused cash cases."""
    unavailable = status == "data_unavailable"
    return {
        "stock_id": int(candidate["stock_id"]),
        "symbol": candidate.get("symbol") or "",
        "name": candidate.get("name") or "",
        "company_name": candidate.get("company_name") or "",
        "industry": candidate.get("industry") or "",
        "scanner_signal_count": int(candidate.get("scanner_signal_count") or 0),
        "starting_capital": round(capital, 2),
        "final_capital": None if unavailable else round(capital, 2),
        "net_pnl": None if unavailable else 0.0,
        "return_pct": None if unavailable else 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "win_rate_pct": None,
        "target_hits": 0,
        "stop_hits": 0,
        "status": status,
        "trades": [],
    }


def _enhanced_backtest_buy_hold_stock(
    candidate: dict[str, Any],
    candles: list[dict[str, Any]],
    snapshot_date: date,
    end_date: date,
    capital_per_stock: float,
    charge_config: DeliveryChargeConfig,
) -> dict[str, Any]:
    """Buy one ranked scanner stock at the next open and sell at the range end."""
    candles, daily_quality = normalize_daily_candles(candles)
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily session(s) for {} during buy-and-hold replay",
            daily_quality["skipped_ambiguous_sessions"],
            candidate.get("symbol", "unknown"),
        )
    forward = sorted(
        (
            candle for candle in candles
            if (candle_date := _enhanced_backtest_date(candle.get("timestamp"))) is not None
            and snapshot_date < candle_date <= end_date
            and (_number(candle.get("open")) or 0) > 0
            and (_number(candle.get("close")) or 0) > 0
        ),
        key=lambda candle: _enhanced_backtest_date(candle.get("timestamp")) or date.max,
    )
    if not forward:
        idle = _enhanced_backtest_idle_stock(candidate, capital_per_stock, "no_forward_candles")
        idle["scanner_signal_count"] = 1
        return idle

    entry_candle = forward[0]
    exit_candle = forward[-1]
    entry_date = _enhanced_backtest_date(entry_candle.get("timestamp"))
    exit_date = _enhanced_backtest_date(exit_candle.get("timestamp"))
    entry_price = _number(entry_candle.get("open"))
    exit_price = _number(exit_candle.get("close"))
    if entry_date is None or exit_date is None or entry_price is None or exit_price is None:
        idle = _enhanced_backtest_idle_stock(candidate, capital_per_stock, "data_unavailable")
        idle["scanner_signal_count"] = 1
        return idle

    quantity = charge_config.max_buy_quantity(capital_per_stock, entry_price)
    if quantity <= 0:
        idle = _enhanced_backtest_idle_stock(candidate, capital_per_stock, "capital_too_small")
        idle["scanner_signal_count"] = 1
        return idle

    buy_charges = charge_config.calculate_order("BUY", entry_price, quantity).total
    sell_charges = charge_config.calculate_order("SELL", exit_price, quantity).total
    gross_pnl = quantity * (exit_price - entry_price)
    net_pnl = gross_pnl - buy_charges - sell_charges
    invested = quantity * entry_price + buy_charges
    starting_capital = float(capital_per_stock)
    final_capital = starting_capital + net_pnl
    trade = {
        "stock_id": candidate.get("stock_id"),
        "symbol": candidate.get("symbol"),
        "name": candidate.get("name") or "",
        "company_name": candidate.get("company_name") or "",
        "rank": candidate.get("rank"),
        "enhanced_score": candidate.get("enhanced_score"),
        "signal_date": snapshot_date.isoformat(),
        "entry_date": entry_date.isoformat(),
        "entry_price": round(entry_price, 4),
        "target_price": None,
        "stop_price": None,
        "target_hit": False,
        "stop_hit": False,
        "exit_date": exit_date.isoformat(),
        "exit_price": round(exit_price, 4),
        "exit_reason": "period_end_sell",
        "quantity": quantity,
        "buy_charges": round(buy_charges, 2),
        "sell_charges": round(sell_charges, 2),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "net_trade_return_pct": round(net_pnl / invested * 100.0, 4) if invested else None,
        "capital_after": round(final_capital, 2),
    }
    return {
        "stock_id": int(candidate["stock_id"]),
        "symbol": candidate.get("symbol") or "",
        "name": candidate.get("name") or "",
        "company_name": candidate.get("company_name") or "",
        "industry": candidate.get("industry") or "",
        "scanner_signal_count": 1,
        "starting_capital": round(starting_capital, 2),
        "final_capital": round(final_capital, 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct": round(net_pnl / starting_capital * 100.0, 4) if starting_capital else None,
        "total_trades": 1,
        "winning_trades": int(net_pnl > 0),
        "win_rate_pct": 100.0 if net_pnl > 0 else 0.0,
        "target_hits": 0,
        "stop_hits": 0,
        "status": "completed",
        "trades": [trade],
    }


def _enhanced_backtest_profile_stock(
    candidate: dict[str, Any],
    preset: Any,
    request: FilterBacktestRequest,
    requirements: dict[str, dict[str, Any]],
    shared_inputs: Any,
    evaluator: FilterEvaluator,
    charge_config: DeliveryChargeConfig,
    entry_signal_date_map: dict[str, str],
) -> dict[str, Any]:
    """Run a selected setup on a fixed scanner-cohort stock after selection."""
    defaults = {
        "stop_loss_pct": preset.default_stop_loss_pct,
        "take_profit_pct": preset.default_take_profit_pct,
        "max_hold_bars": preset.max_hold_bars,
    }
    _, stock, _, _, result = _run_filter_backtest_expression(
        preset.expression,
        request,
        requirements,
        exit_defaults=defaults,
        execution_timeframe=preset.timeframe,
        shared_inputs=shared_inputs,
        evaluator=evaluator,
        charge_config=charge_config,
        entry_signal_date_map=entry_signal_date_map,
    )
    stock_result = {
        "stock_id": int(stock["id"]),
        "symbol": stock["symbol"],
        "name": stock.get("name") or candidate.get("name") or "",
        "company_name": _stock_company_name(stock["symbol"], stock.get("name")) or "",
        "industry": candidate.get("industry") or "",
        "scanner_signal_count": int(candidate.get("scanner_signal_count") or 0),
        "starting_capital": round(float(result["initial_capital"]), 2),
        "final_capital": round(float(result["final_capital"]), 2),
        "net_pnl": round(float(result["final_capital"]) - float(result["initial_capital"]), 2),
        "return_pct": round(float(result["total_return_pct"]), 4),
        "total_trades": int(result["total_trades"]),
        "winning_trades": int(result["winning_trades"]),
        "win_rate_pct": round(float(result["win_rate_pct"]), 2),
        "target_hits": sum(trade.get("exit_reason") == "take_profit" for trade in result["trades"]),
        "stop_hits": sum(trade.get("exit_reason") == "stop_loss" for trade in result["trades"]),
        "status": "completed" if result["total_trades"] else "no_setup_trigger",
        "trades": [],
    }
    for trade in result["trades"]:
        entry_price = float(trade["entry_price"])
        stock_result["trades"].append({
            "signal_date": trade.get("scanner_signal_date"),
            "entry_date": trade["entry_date"],
            "entry_price": entry_price,
            "target_price": round(entry_price * (1 + preset.default_take_profit_pct / 100), 4),
            "stop_price": round(entry_price * (1 - preset.default_stop_loss_pct / 100), 4),
            "target_hit": trade.get("exit_reason") == "take_profit",
            "stop_hit": trade.get("exit_reason") == "stop_loss",
            "exit_date": trade["exit_date"],
            "exit_price": trade["exit_price"],
            "exit_reason": trade["exit_reason"],
            "quantity": int(trade["quantity"]),
            "gross_pnl": round(float(trade["gross_pnl"]), 2),
            "total_charges": round(float(trade["total_charges"]), 2),
            "net_pnl": round(float(trade["pnl"]), 2),
            "net_trade_return_pct": round(float(trade["pnl_pct"]), 4),
            "hold_bars": int(trade["hold_bars"]),
        })
    return stock_result


def _enhanced_backtest_selected_stock_batch(
    candidate: dict[str, Any],
    preset_ids: list[str],
    start_date: str,
    end_date: str,
    capital_per_stock: float,
    aggregate_requirements: dict[str, dict[str, Any]],
    requirements_by_preset: dict[str, dict[str, Any]],
    charge_values: dict[str, Any],
    entry_signal_date_map: dict[str, str],
    fundamental_setup_ids: list[str],
) -> dict[str, Any]:
    """Evaluate all selected replay setups for a stock from one candle load."""
    presets_by_id = {preset.preset_id: preset for preset in build_filter_presets()}
    selected_presets = [presets_by_id[preset_id] for preset_id in preset_ids]
    output: dict[str, Any] = {
        "results": {},
        "errors": {},
        "fundamental_setup_results": {},
        "fundamental_setup_errors": {},
    }
    filter_request = FilterBacktestRequest(
        symbol=str(candidate.get("symbol") or ""),
        start_date=start_date,
        end_date=end_date,
        initial_capital=capital_per_stock,
        position_size_pct=100,
    )

    try:
        history_warmup_by_timeframe = _enhanced_backtest_filter_lookbacks(selected_presets)
        if fundamental_setup_ids:
            # The 52-week setup needs 252 full sessions of five-minute history.
            five_minute_setup_warmup = 252 * INTRADAY_BARS_PER_TRADING_DAY
            history_warmup_by_timeframe[Timeframe.FIVE_MINUTE.value] = max(
                history_warmup_by_timeframe.get(Timeframe.FIVE_MINUTE.value, 0),
                five_minute_setup_warmup,
            )
        shared_inputs = _load_filter_backtest_inputs(
            filter_request,
            aggregate_requirements,
            execution_timeframe=Timeframe.FIVE_MINUTE,
            warmup_candles_by_timeframe=history_warmup_by_timeframe,
        )
        candles_by_timeframe = shared_inputs[4]
        if selected_presets:
            expressions = [preset.expression for preset in selected_presets]
            atomic_cache = build_atomic_backtest_cache(expressions, candles_by_timeframe, _FILTER_CATALOG)
            evaluator = FilterEvaluator(_FILTER_CATALOG, atomic_cache=atomic_cache)
        else:
            evaluator = None
        charge_config = DeliveryChargeConfig.from_mapping(charge_values)
    except Exception as error:
        detail = str(error.detail if isinstance(error, HTTPException) else error)
        for preset in selected_presets:
            output["results"][preset.preset_id] = _enhanced_backtest_idle_stock(
                candidate,
                capital_per_stock,
                "data_unavailable",
            )
            output["errors"][preset.preset_id] = detail
        for setup_id in fundamental_setup_ids:
            output["fundamental_setup_results"][setup_id] = _enhanced_backtest_idle_stock(
                candidate,
                capital_per_stock,
                "data_unavailable",
            )
            output["fundamental_setup_errors"][setup_id] = detail
        return output

    for preset in selected_presets:
        try:
            output["results"][preset.preset_id] = _enhanced_backtest_profile_stock(
                candidate,
                preset,
                filter_request,
                requirements_by_preset[preset.preset_id],
                shared_inputs,
                evaluator,
                charge_config,
                entry_signal_date_map,
            )
        except Exception as error:
            output["results"][preset.preset_id] = _enhanced_backtest_idle_stock(
                candidate,
                capital_per_stock,
                "data_unavailable",
            )
            output["errors"][preset.preset_id] = str(error.detail if isinstance(error, HTTPException) else error)

    if fundamental_setup_ids:
        try:
            points = candles_by_timeframe.get(Timeframe.FIVE_MINUTE, [])
            by_timestamp: dict[datetime, CandlePoint] = {}
            for point in points:
                previous = by_timestamp.get(point.timestamp)
                if previous is None or point.volume > previous.volume:
                    by_timestamp[point.timestamp] = point
            setup_candles = [
                {
                    "timestamp": point.timestamp,
                    "open": point.open,
                    "high": point.high,
                    "low": point.low,
                    "close": point.close,
                    "volume": point.volume,
                }
                for point in (by_timestamp[timestamp] for timestamp in sorted(by_timestamp))
            ]
            if len(setup_candles) < 2:
                raise RuntimeError("No five-minute candles are available in the requested period.")
            candle_series = prepare_setup_candles(setup_candles)
        except Exception as error:
            detail = str(error.detail if isinstance(error, HTTPException) else error)
            for setup_id in fundamental_setup_ids:
                output["fundamental_setup_results"][setup_id] = _enhanced_backtest_idle_stock(
                    candidate,
                    capital_per_stock,
                    "data_unavailable",
                )
                output["fundamental_setup_errors"][setup_id] = detail
            return output

        setup_start = date.fromisoformat(start_date)
        setup_end = date.fromisoformat(end_date)
        for setup_id in fundamental_setup_ids:
            try:
                result = run_setup(
                    candles=setup_candles,
                    candle_series=candle_series,
                    setup_id=setup_id,
                    start_date=setup_start,
                    end_date=setup_end,
                    initial_capital=capital_per_stock,
                    charge_config=charge_config,
                    target_r=2.0,
                    max_hold_bars=1_500,
                    stop_buffer_pct=0.5,
                    entry_signal_date_map=entry_signal_date_map,
                )
                trades = []
                for trade in result["trades"]:
                    signal_date = str(trade.get("signal_date") or "")
                    exit_reason = str(trade.get("exit_reason") or "")
                    trades.append({
                        "signal_date": signal_date,
                        "scanner_signal_date": entry_signal_date_map.get(signal_date[:10]),
                        "entry_date": trade["entry_date"],
                        "entry_price": trade["entry_price"],
                        "target_price": trade.get("target_price"),
                        "stop_price": trade.get("stop_price"),
                        "target_hit": exit_reason.startswith("target") or exit_reason == "gap_target",
                        "stop_hit": exit_reason in {"stop_loss", "gap_stop"},
                        "exit_date": trade["exit_date"],
                        "exit_price": trade["exit_price"],
                        "exit_reason": exit_reason,
                        "quantity": int(trade.get("quantity") or 0),
                        "gross_pnl": round(float(trade.get("pnl") or 0) + float(trade.get("total_charges") or 0), 2),
                        "total_charges": float(trade.get("total_charges") or 0),
                        "net_pnl": float(trade.get("pnl") or 0),
                        "net_trade_return_pct": float(trade.get("return_pct") or 0),
                    })
                metrics = result["metrics"]
                output["fundamental_setup_results"][setup_id] = {
                    "stock_id": int(candidate["stock_id"]),
                    "symbol": candidate.get("symbol") or "",
                    "name": candidate.get("name") or "",
                    "company_name": candidate.get("company_name") or "",
                    "industry": candidate.get("industry") or "",
                    "scanner_signal_count": int(candidate.get("scanner_signal_count") or 0),
                    "starting_capital": round(float(capital_per_stock), 2),
                    "final_capital": round(float(metrics["final_capital"]), 2),
                    "net_pnl": round(float(metrics["final_capital"]) - float(capital_per_stock), 2),
                    "return_pct": round(float(metrics["total_return_pct"]), 4),
                    "total_trades": len(trades),
                    "winning_trades": sum(float(trade["net_pnl"]) > 0 for trade in trades),
                    "win_rate_pct": round(float(metrics["win_rate_pct"]), 2),
                    "target_hits": sum(bool(trade["target_hit"]) for trade in trades),
                    "stop_hits": sum(bool(trade["stop_hit"]) for trade in trades),
                    "status": "completed" if trades else "no_setup_trigger",
                    "trades": trades,
                }
            except Exception as error:
                detail = str(error.detail if isinstance(error, HTTPException) else error)
                output["fundamental_setup_results"][setup_id] = _enhanced_backtest_idle_stock(
                    candidate,
                    capital_per_stock,
                    "data_unavailable",
                )
                output["fundamental_setup_errors"][setup_id] = detail
    return output


def _enhanced_backtest_filter_lookbacks(presets) -> dict[str, int]:
    """Get exact pre-window candle counts needed by selected filter expressions."""
    lookbacks: dict[str, int] = {}

    def visit(node) -> None:
        if isinstance(node, FilterGroup):
            for child in node.children:
                visit(child)
            return

        requirement = _FILTER_CATALOG.requirements_for(
            node.filter_id,
            node.params,
            timeframe=node.timeframe,
        )
        required_candles = requirement.total_candles
        if node.filter_id == "data_completeness":
            required_candles = max(
                required_candles,
                int(node.params.get("minimum_candles", 30)),
            )
        timeframe = node.timeframe.value
        lookbacks[timeframe] = max(lookbacks.get(timeframe, 0), required_candles)

    for preset in presets:
        for node in preset.expression.children + preset.expression.veto:
            visit(node)
    return lookbacks


def _enhanced_backtest_strategy_summary(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated_stocks = [stock for stock in stocks if stock.get("status") != "data_unavailable"]
    returns = [float(stock["return_pct"]) for stock in evaluated_stocks if stock.get("return_pct") is not None]
    ordered_returns = sorted(returns)
    middle = len(ordered_returns) // 2
    median_return = (
        ordered_returns[middle] if len(ordered_returns) % 2
        else (ordered_returns[middle - 1] + ordered_returns[middle]) / 2
    ) if ordered_returns else None
    starting_capital = sum(float(stock.get("starting_capital") or 0) for stock in evaluated_stocks)
    final_capital = sum(float(stock.get("final_capital") or 0) for stock in evaluated_stocks)
    net_pnl = final_capital - starting_capital
    trades = sum(int(stock.get("total_trades") or 0) for stock in stocks)
    target_eligible = sum(
        trade.get("target_price") is not None
        for stock in stocks
        for trade in stock.get("trades", [])
    )
    target_hits = sum(int(stock.get("target_hits") or 0) for stock in stocks)
    stop_hits = sum(int(stock.get("stop_hits") or 0) for stock in stocks)
    return {
        "stock_count": len(stocks),
        "evaluated_stocks": len(evaluated_stocks),
        "stocks_traded": sum(int(stock.get("total_trades") or 0) > 0 for stock in evaluated_stocks),
        "average_stock_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
        "median_stock_return_pct": round(median_return, 4) if median_return is not None else None,
        "positive_stocks": sum(value > 0 for value in returns),
        "positive_stock_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100.0, 2) if returns else None,
        "starting_capital": round(starting_capital, 2),
        "final_capital": round(final_capital, 2),
        "net_pnl": round(net_pnl, 2),
        "total_return_pct": round(net_pnl / starting_capital * 100.0, 4) if starting_capital else None,
        "total_trades": trades,
        "target_eligible_trades": target_eligible,
        "target_hits": target_hits,
        "target_hit_rate_pct": round(target_hits / target_eligible * 100.0, 2) if target_eligible else None,
        "stop_hits": stop_hits,
    }


def _enhanced_backtest_portfolio_replay(
    stock_results: list[dict[str, Any]],
    candidates_by_stock: dict[int, list[dict[str, Any]]],
    rank_by_stock_snapshot: dict[tuple[int, str], int],
    *,
    slot_count: int,
    capital_per_slot: float,
    charge_config: DeliveryChargeConfig,
    end_date: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay candidate trades through a fixed shared pool of reusable capital slots."""
    import heapq

    candidate_by_stock_id = {
        stock_id: sorted(rows, key=lambda row: _enhanced_backtest_date(row.get("as_of_date")) or date.min)[-1]
        for stock_id, rows in candidates_by_stock.items()
        if rows
    }
    stock_rows: dict[int, dict[str, Any]] = {}
    candidate_status: dict[int, str] = {}
    for stock in stock_results:
        stock_id = int(stock.get("stock_id") or 0)
        if stock_id:
            candidate_status[stock_id] = str(stock.get("status") or "no_trade")

    events: list[dict[str, Any]] = []
    for stock in stock_results:
        stock_id = int(stock.get("stock_id") or 0)
        if not stock_id:
            continue
        for source_trade in stock.get("trades") or []:
            trade = dict(source_trade)
            entry_time = _enhanced_backtest_timestamp(trade.get("entry_date"), date_time=time(9, 15))
            exit_time = _enhanced_backtest_timestamp(trade.get("exit_date"), date_time=time(15, 30))
            entry_price = _number(trade.get("entry_price"))
            exit_price = _number(trade.get("exit_price"))
            if entry_time is None or exit_time is None or entry_price is None or exit_price is None:
                continue
            if exit_time < entry_time or entry_price <= 0 or exit_price <= 0:
                continue
            snapshot_date = str(trade.get("scanner_signal_date") or trade.get("signal_date") or "")[:10]
            rank = rank_by_stock_snapshot.get((stock_id, snapshot_date))
            if rank is None:
                rank = int(trade.get("rank") or 10**9)
            candidate = candidate_by_stock_id.get(stock_id, {})
            events.append({
                "stock_id": stock_id,
                "symbol": trade.get("symbol") or stock.get("symbol") or candidate.get("symbol") or "",
                "candidate": candidate,
                "trade": trade,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "rank": rank,
                "snapshot_date": snapshot_date,
            })

    events.sort(key=lambda item: (item["entry_time"], item["rank"], item["symbol"]))
    slot_cash = [float(capital_per_slot) for _ in range(max(0, slot_count))]
    open_slots: dict[int, dict[str, Any]] = {}
    open_symbols: set[int] = set()
    exit_heap: list[tuple[datetime, int, int, dict[str, Any]]] = []
    next_sequence = 0
    max_concurrent_positions = 0
    skipped_for_capacity = 0
    skipped_for_symbol_overlap = 0
    skipped_for_capital = 0
    blocked_by_stock: dict[int, int] = {}
    executed_trades: list[dict[str, Any]] = []

    def stock_row(stock_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
        if stock_id not in stock_rows:
            stock_rows[stock_id] = {
                "stock_id": stock_id,
                "symbol": candidate.get("symbol") or "",
                "name": candidate.get("name") or "",
                "company_name": candidate.get("company_name") or "",
                "industry": candidate.get("industry") or "",
                "rank": candidate.get("rank"),
                "enhanced_score": candidate.get("enhanced_score"),
                "scanner_signal_count": len(candidates_by_stock.get(stock_id, [])),
                "starting_capital": 0.0,
                "final_capital": 0.0,
                "net_pnl": 0.0,
                "return_pct": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "win_rate_pct": None,
                "target_hits": 0,
                "stop_hits": 0,
                "status": candidate_status.get(stock_id, "no_trade"),
                "trades": [],
            }
        return stock_rows[stock_id]

    def close_positions_through(timestamp: datetime) -> None:
        while exit_heap and exit_heap[0][0] <= timestamp:
            exit_time, _, slot_index, position = heapq.heappop(exit_heap)
            if open_slots.get(slot_index) is not position:
                continue
            slot_cash[slot_index] += position["exit_proceeds"]
            open_slots.pop(slot_index, None)
            open_symbols.discard(position["stock_id"])
            realized = position["executed_trade"]
            realized["capital_after"] = round(slot_cash[slot_index], 2)
            realized["exit_timestamp"] = exit_time.isoformat()

    for event in events:
        close_positions_through(event["entry_time"])
        stock_id = event["stock_id"]
        if stock_id in open_symbols:
            skipped_for_symbol_overlap += 1
            blocked_by_stock[stock_id] = blocked_by_stock.get(stock_id, 0) + 1
            continue
        free_slot = next((index for index in range(len(slot_cash)) if index not in open_slots), None)
        if free_slot is None:
            skipped_for_capacity += 1
            blocked_by_stock[stock_id] = blocked_by_stock.get(stock_id, 0) + 1
            continue

        entry_price = event["entry_price"]
        exit_price = event["exit_price"]
        available_cash = slot_cash[free_slot]
        quantity = charge_config.max_buy_quantity(available_cash, entry_price)
        if quantity <= 0:
            skipped_for_capital += 1
            blocked_by_stock[stock_id] = blocked_by_stock.get(stock_id, 0) + 1
            continue
        buy_charges = charge_config.calculate_order("BUY", entry_price, quantity).total
        invested = quantity * entry_price + buy_charges
        if invested > available_cash + 1e-6:
            skipped_for_capital += 1
            blocked_by_stock[stock_id] = blocked_by_stock.get(stock_id, 0) + 1
            continue
        sell_charges = charge_config.calculate_order("SELL", exit_price, quantity).total
        gross_pnl = quantity * (exit_price - entry_price)
        net_pnl = gross_pnl - buy_charges - sell_charges
        exit_proceeds = quantity * exit_price - sell_charges
        source_trade = event["trade"]
        candidate = event["candidate"]
        realized_trade = {
            **source_trade,
            "stock_id": stock_id,
            "symbol": event["symbol"],
            "rank": event["rank"],
            "enhanced_score": candidate.get("enhanced_score", source_trade.get("enhanced_score")),
            "quantity": quantity,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "buy_charges": round(buy_charges, 2),
            "sell_charges": round(sell_charges, 2),
            "total_charges": round(buy_charges + sell_charges, 2),
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "pnl": round(net_pnl, 2),
            "net_trade_return_pct": round(net_pnl / invested * 100.0, 4) if invested else None,
            "capital_slot": free_slot + 1,
            "invested_capital": round(invested, 2),
        }
        slot_cash[free_slot] -= invested
        stock_result = stock_row(stock_id, candidate)
        stock_result["starting_capital"] += invested
        stock_result["net_pnl"] += net_pnl
        stock_result["total_trades"] += 1
        stock_result["winning_trades"] += int(net_pnl > 0)
        stock_result["target_hits"] += int(bool(source_trade.get("target_hit")))
        stock_result["stop_hits"] += int(bool(source_trade.get("stop_hit")))
        stock_result["status"] = "completed"
        stock_result["trades"].append(realized_trade)
        executed_trades.append(realized_trade)

        position = {
            "stock_id": stock_id,
            "exit_proceeds": exit_proceeds,
            "executed_trade": realized_trade,
        }
        open_slots[free_slot] = position
        open_symbols.add(stock_id)
        next_sequence += 1
        heapq.heappush(exit_heap, (event["exit_time"], next_sequence, free_slot, position))
        max_concurrent_positions = max(max_concurrent_positions, len(open_slots))

    close_positions_through(datetime.max.replace(tzinfo=IST))

    for stock_id, candidate in candidate_by_stock_id.items():
        result = stock_row(stock_id, candidate)
        result["final_capital"] = round(float(result["starting_capital"]) + float(result["net_pnl"]), 2)
        result["return_pct"] = (
            round(float(result["net_pnl"]) / float(result["starting_capital"]) * 100.0, 4)
            if result["starting_capital"]
            else None if result["status"] == "data_unavailable" else 0.0
        )
        result["win_rate_pct"] = (
            round(int(result["winning_trades"]) / int(result["total_trades"]) * 100.0, 2)
            if result["total_trades"]
            else None
        )
        if result["total_trades"] == 0 and result["status"] != "data_unavailable":
            result["status"] = "capital_unavailable" if blocked_by_stock.get(stock_id) else "no_setup_trigger"
    normalized_stocks = list(stock_rows.values())
    normalized_stocks.sort(key=lambda row: (row.get("return_pct") is None, -(row.get("return_pct") or 0), row.get("symbol") or ""))
    starting_capital = float(slot_count) * float(capital_per_slot)
    final_capital = sum(slot_cash)
    net_pnl = final_capital - starting_capital
    trade_returns = [float(trade["net_trade_return_pct"]) for trade in executed_trades if trade.get("net_trade_return_pct") is not None]
    stock_returns = sorted(float(result["return_pct"]) for result in normalized_stocks if result.get("return_pct") is not None)
    if stock_returns:
        middle = len(stock_returns) // 2
        median_stock_return = stock_returns[middle] if len(stock_returns) % 2 else (stock_returns[middle - 1] + stock_returns[middle]) / 2
    else:
        median_stock_return = None
    target_eligible = sum(trade.get("target_price") is not None for trade in executed_trades)
    target_hits = sum(bool(trade.get("target_hit")) for trade in executed_trades)
    summary = {
        "stock_count": len(candidate_by_stock_id),
        "evaluated_stocks": sum(result.get("status") != "data_unavailable" for result in normalized_stocks),
        "stocks_traded": sum(int(result.get("total_trades") or 0) > 0 for result in normalized_stocks),
        "average_stock_return_pct": round(sum(float(result["return_pct"]) for result in normalized_stocks if result.get("return_pct") is not None) / max(1, sum(result.get("return_pct") is not None for result in normalized_stocks)), 4) if normalized_stocks else None,
        "median_stock_return_pct": round(median_stock_return, 4) if median_stock_return is not None else None,
        "positive_stocks": sum(float(result.get("net_pnl") or 0) > 0 for result in normalized_stocks),
        "positive_stock_rate_pct": round(sum(float(result.get("net_pnl") or 0) > 0 for result in normalized_stocks) / max(1, sum(result.get("total_trades", 0) > 0 for result in normalized_stocks)) * 100, 2) if any(result.get("total_trades", 0) for result in normalized_stocks) else None,
        "starting_capital": round(starting_capital, 2),
        "final_capital": round(final_capital, 2),
        "net_pnl": round(net_pnl, 2),
        "total_return_pct": round(net_pnl / starting_capital * 100.0, 4) if starting_capital else None,
        "total_trades": len(executed_trades),
        "target_eligible_trades": target_eligible,
        "target_hits": target_hits,
        "target_hit_rate_pct": round(target_hits / target_eligible * 100.0, 2) if target_eligible else None,
        "stop_hits": sum(bool(trade.get("stop_hit")) for trade in executed_trades),
        "average_net_trade_return_pct": round(sum(trade_returns) / len(trade_returns), 4) if trade_returns else None,
        "capital_slots": slot_count,
        "maximum_concurrent_positions": max_concurrent_positions,
        "blocked_trade_signals": skipped_for_capacity + skipped_for_symbol_overlap + skipped_for_capital,
        "blocked_for_full_capital": skipped_for_capacity,
        "blocked_for_same_stock_position": skipped_for_symbol_overlap,
        "blocked_for_insufficient_slot_cash": skipped_for_capital,
    }
    return normalized_stocks, summary


_ENHANCED_BACKTEST_RUN_COLUMNS = (
    "signal_cutoff_date", "end_date", "lookback_months", "filters_json",
    "config_json", "summary_json", "scan_json", "results_json", "strategies_json", "job_id",
)


def _save_enhanced_backtest_run(db: Database, values: tuple, *, job_id: str | None) -> dict[str, Any]:
    """Insert a finished replay, fenced on job ownership when run by a job.

    The data-modifying CTE updates the job row first, which takes a row lock:
    a concurrent settlement of the same job either commits before (then the
    job is no longer running and nothing is inserted) or waits and then sees
    the refreshed heartbeat (and does not settle). Either way a late worker
    cannot save a result for a job it no longer owns.
    """
    columns = ", ".join(_ENHANCED_BACKTEST_RUN_COLUMNS)
    placeholders = ", ".join("%s" for _ in _ENHANCED_BACKTEST_RUN_COLUMNS)
    if job_id is None:
        return db.execute_returning(
            f"INSERT INTO enhanced_momentum_backtest_runs ({columns}) VALUES ({placeholders}) RETURNING id, created_at",
            (*values, None),
        )

    def saved_run() -> dict[str, Any] | None:
        return db.fetch_one(
            "SELECT id, created_at FROM enhanced_momentum_backtest_runs WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        )

    # Saving is idempotent per job: the unique index on runs(job_id) turns a
    # repeat into ON CONFLICT DO NOTHING, and the saved row is re-read. So an
    # insert whose reply was lost can simply be tried once more with the
    # same already-computed values; nothing is recalculated or duplicated.
    statement = f"""
        WITH owner AS (
            UPDATE enhanced_momentum_backtest_jobs
            SET heartbeat_at = NOW()
            WHERE job_id = %s AND worker_id = %s AND status = 'running'
            RETURNING job_id
        )
        INSERT INTO enhanced_momentum_backtest_runs ({columns})
        SELECT {placeholders} FROM owner
        ON CONFLICT (job_id) WHERE job_id IS NOT NULL DO NOTHING
        RETURNING id, created_at
        """
    params = (job_id, _ENHANCED_BACKTEST_WORKER_ID, *values, job_id)
    try:
        row = db.execute_returning(statement, params)
    except DatabaseError as error:
        logger.warning("Result save for enhanced momentum replay job {} did not return; retrying once: {}", job_id, error)
        row = db.execute_returning(statement, params)
    if row is None:
        row = saved_run()
    if row is None:
        raise _EnhancedBacktestOwnershipLost(job_id)
    return row


def _enhanced_backtest_summary(results: list[dict[str, Any]], scan: dict[str, Any]) -> dict[str, Any]:
    entry_candidates = [row for row in results if row.get("entry_date")]
    executed = [row for row in entry_candidates if int(row.get("quantity") or 0) > 0]
    completed = [row for row in entry_candidates if row.get("horizon_complete")]
    horizon_returns = [float(row["horizon_return_pct"]) for row in completed if row.get("horizon_return_pct") is not None]
    resolved_trades = [row for row in executed if row.get("horizon_complete") or row.get("target_hit") or row.get("stop_hit")]
    net_returns = [float(row["net_trade_return_pct"]) for row in resolved_trades if row.get("net_trade_return_pct") is not None]
    net_pnls = [float(row["net_pnl"]) for row in resolved_trades if row.get("net_pnl") is not None]
    target_eligible = [row for row in resolved_trades if row.get("target_price") is not None]
    count = len(executed)
    mean_horizon = sum(horizon_returns) / len(horizon_returns) if horizon_returns else None
    mean_net = sum(net_returns) / len(net_returns) if net_returns else None
    invested = sum(float(row.get("quantity") or 0) * float(row.get("entry_price") or 0) + float(row.get("buy_charges") or 0) for row in resolved_trades)
    ordered_horizon_returns = sorted(horizon_returns)
    middle = len(ordered_horizon_returns) // 2
    median_horizon = (
        ordered_horizon_returns[middle] if len(ordered_horizon_returns) % 2
        else (ordered_horizon_returns[middle - 1] + ordered_horizon_returns[middle]) / 2
    ) if ordered_horizon_returns else None
    total_net = sum(net_pnls) if net_pnls else None
    return {
        "matching_stocks": int(scan.get("matching_stocks") or 0),
        "returned_stocks": len(results),
        "entry_candidates": len(entry_candidates),
        "executed_trades": count,
        "completed_forward_windows": len(completed),
        "partial_forward_windows": len(entry_candidates) - len(completed),
        "average_horizon_return_pct": round(mean_horizon, 4) if mean_horizon is not None else None,
        "median_horizon_return_pct": round(median_horizon, 4) if median_horizon is not None else None,
        "positive_horizon_trades": sum(value > 0 for value in horizon_returns),
        "horizon_win_rate_pct": round(sum(value > 0 for value in horizon_returns) / len(horizon_returns) * 100.0, 2) if horizon_returns else None,
        "target_eligible_trades": len(target_eligible),
        "target_hits": sum(bool(row.get("target_hit")) for row in target_eligible),
        "target_hit_rate_pct": round(sum(bool(row.get("target_hit")) for row in target_eligible) / len(target_eligible) * 100.0, 2) if target_eligible else None,
        "stop_hits": sum(bool(row.get("stop_hit")) for row in target_eligible),
        "average_net_trade_return_pct": round(mean_net, 4) if mean_net is not None else None,
        "aggregate_net_pnl": round(total_net, 2) if total_net is not None else None,
        "aggregate_invested_capital": round(invested, 2),
        "aggregate_net_return_pct": round(total_net / invested * 100.0, 4) if total_net is not None and invested > 0 else None,
    }


def _execute_enhanced_momentum_backtest(
    request: EnhancedMomentumBacktestRequest,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    job_id: str | None = None,
):
    """Run the replay and report phase progress as each unit of work completes."""
    import json as _json

    def report_progress(
        percent: float,
        phase: str,
        completed: int = 0,
        total: int = 0,
        unit: str = "steps",
        detail: str = "",
    ) -> None:
        if progress_callback is not None:
            progress_callback({
                "percent": round(max(0.0, min(percent, 99.0)), 1),
                "phase": phase,
                "completed": completed,
                "total": total,
                "unit": unit,
                "detail": detail,
            })

    db = get_db()
    signal_cutoff, end_date, stored_months = _enhanced_backtest_window(request)
    historical_filters = request.filters.model_dump(exclude={"as_of_date"})
    preset_ids = list(dict.fromkeys(request.preset_ids))
    available_presets = {preset.preset_id: preset for preset in build_filter_presets()}
    unknown_presets = [preset_id for preset_id in preset_ids if preset_id not in available_presets]
    if unknown_presets:
        raise HTTPException(status_code=422, detail=f"Unknown Watchlist Analysis filter(s): {', '.join(unknown_presets)}")
    selected_presets = [available_presets[preset_id] for preset_id in preset_ids]
    fundamental_setup_ids = list(dict.fromkeys(request.fundamental_setup_ids))
    invalid_fundamental_setups = [
        setup_id
        for setup_id in fundamental_setup_ids
        if setup_id not in SETUPS or setup_id in FILTER_BACKED_SETUP_IDS
    ]
    if invalid_fundamental_setups:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or filter-backed Fundamental Setup(s): {', '.join(invalid_fundamental_setups)}",
        )

    start_inclusive = datetime.combine(signal_cutoff, datetime.min.time(), tzinfo=IST)
    end_exclusive = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    session_rows = db.fetch_all(
        """
        SELECT DISTINCT (timestamp AT TIME ZONE 'Asia/Kolkata')::date AS session_date
        FROM candles_1d
        WHERE timestamp >= %s AND timestamp < %s
        ORDER BY session_date
        """,
        (start_inclusive, end_exclusive),
    )
    session_dates = [
        parsed
        for row in session_rows
        if (parsed := _enhanced_backtest_date(row.get("session_date"))) is not None
    ]
    cohort_dates = _enhanced_momentum_refresh_dates(
        session_dates,
        signal_cutoff,
        end_date,
        request.refresh_frequency,
    )
    cohort_date = cohort_dates[0] if cohort_dates else None
    historical_request = EnhancedMomentumScanRequest(**historical_filters, as_of_date=cohort_date or signal_cutoff)
    report_progress(
        3,
        "Preparing scanner refresh schedule",
        0,
        len(cohort_dates),
        "refreshes",
        f"Found {len(session_dates)} sessions and {len(cohort_dates)} scheduled {request.refresh_frequency} scans",
    )

    # Every cohort is a point-in-time ranked snapshot. Its names remain eligible
    # for setup entries until the next refresh is known; positions already open
    # at that refresh remain in the replay until their own exits.
    candidates_by_stock: dict[int, list[dict[str, Any]]] = {}
    scanner_signal_count = 0
    matching_stock_sessions = 0
    snapshot_returned_stocks = 0
    stale_snapshot_results_excluded = 0
    fresh_snapshot_match_count = 0
    session_summaries: list[dict[str, Any]] = []
    cohort_batches: list[dict[str, Any]] = []
    rank_by_stock_snapshot: dict[tuple[int, str], int] = {}
    profile_gates: dict[int, dict[str, str]] = {}
    latest_scan: dict[str, Any] | None = None
    scan_results_by_date: dict[date, dict[str, Any]] = {}
    if len(cohort_dates) == 1:
        only_date = cohort_dates[0]
        report_progress(5, "Scanning refreshed momentum cohorts", 0, 1, "refreshes", f"Running Enhanced Momentum as of {only_date.isoformat()}")
        scan_results_by_date[only_date] = _enhanced_momentum_scan_session_worker(only_date, historical_filters)
    elif cohort_dates:
        scan_workers = min(_MAX_ENHANCED_MOMENTUM_BACKTEST_WORKERS, len(cohort_dates))
        with ProcessPoolExecutor(
            max_workers=scan_workers,
            mp_context=get_context("spawn"),
            initializer=_initialize_cpu_analysis_worker,
        ) as executor:
            scan_futures = {
                executor.submit(_enhanced_momentum_scan_session_worker, scan_date, historical_filters): scan_date
                for scan_date in cohort_dates
            }
            for scan_index_completed, future in enumerate(as_completed(scan_futures), start=1):
                scan_date = scan_futures[future]
                scan_results_by_date[scan_date] = future.result()
                report_progress(
                    5 + 42 * scan_index_completed / len(cohort_dates),
                    "Scanning refreshed momentum cohorts",
                    scan_index_completed,
                    len(cohort_dates),
                    "refreshes",
                    f"Finished Enhanced Momentum snapshot for {scan_date.isoformat()}",
                )
    for cohort_index, scan_date in enumerate(cohort_dates):
        latest_scan = scan_results_by_date[scan_date]
        matching_stock_sessions = int(latest_scan.get("matching_stocks") or 0)
        scanner_results = latest_scan.get("results") or []
        this_snapshot_returned = int(
            latest_scan.get("returned_stocks") if latest_scan.get("returned_stocks") is not None else len(scanner_results)
        )
        if cohort_index == 0:
            snapshot_returned_stocks = this_snapshot_returned
        session_summary = {
            "date": scan_date.isoformat(),
            "matching_stocks": matching_stock_sessions,
            "returned_stocks": this_snapshot_returned,
            "market_regime": (latest_scan.get("market_context") or {}).get("regime"),
        }
        session_summaries.append(session_summary)
        fresh_candidates: list[dict[str, Any]] = []
        for candidate in scanner_results:
            signal_date = _enhanced_backtest_date(candidate.get("as_of_date"))
            if signal_date != scan_date:
                stale_snapshot_results_excluded += 1
                continue
            fresh_candidates.append(dict(candidate))
        fresh_candidates.sort(
            key=lambda row: (
                int(row.get("rank") or 10**9),
                -(float(row.get("enhanced_score") or 0)),
                str(row.get("symbol") or ""),
            )
        )
        fresh_snapshot_match_count += len(fresh_candidates)
        if request.filters.top_n is not None:
            fresh_candidates = fresh_candidates[: request.filters.top_n]
        for rank, candidate in enumerate(fresh_candidates, start=1):
            candidate["rank"] = rank
            candidate["_selection_date"] = scan_date.isoformat()
            stock_id = int(candidate["stock_id"])
            candidates_by_stock.setdefault(stock_id, []).append(candidate)
            rank_by_stock_snapshot[(stock_id, scan_date.isoformat())] = rank
            scanner_signal_count += 1
        cohort_batches.append({"date": scan_date, "candidates": fresh_candidates})
        report_progress(
            5 + 42 * (cohort_index + 1) / max(len(cohort_dates), 1),
            "Refreshed momentum cohort selected",
            cohort_index + 1,
            len(cohort_dates),
            "refreshes",
            f"Selected {len(fresh_candidates)} fresh stocks from {matching_stock_sessions} matches on {scan_date.isoformat()}",
        )

    session_index = {session: index for index, session in enumerate(session_dates)}
    for cohort_index, batch in enumerate(cohort_batches):
        scan_date = batch["date"]
        scan_index = session_index.get(scan_date)
        if scan_index is None:
            continue
        next_scan_index = (
            session_index.get(cohort_batches[cohort_index + 1]["date"])
            if cohort_index + 1 < len(cohort_batches)
            else len(session_dates)
        )
        active_sessions = session_dates[scan_index + 1 : (next_scan_index + 1 if next_scan_index is not None else len(session_dates))]
        for candidate in batch["candidates"]:
            stock_id = int(candidate["stock_id"])
            gate = profile_gates.setdefault(stock_id, {})
            for active_session in active_sessions:
                gate[active_session.isoformat()] = scan_date.isoformat()

    max_cohort_size = max((len(batch["candidates"]) for batch in cohort_batches), default=0)
    slot_count = request.filters.top_n if request.filters.top_n is not None else max_cohort_size

    report_progress(48, "Preparing candle history", 0, len(candidates_by_stock), "stocks", f"Loading daily candles for {len(candidates_by_stock)} stocks across refreshed cohorts")
    charge_config = config_from_store(store("config"))
    # Generate price-path opportunities independently of the user's capital
    # slot size. The shared portfolio replay applies the real slot balance and
    # costs when deciding whether each entry can actually be funded.
    signal_generation_capital = max(float(request.capital_per_stock), 1_000_000_000.0)
    candidate_ids = list(candidates_by_stock)
    candle_rows: list[dict[str, Any]] = []
    if candidate_ids:
        candle_rows = db.fetch_all(
            """
            SELECT stock_id, timestamp, open, high, low, close
            FROM candles_1d
            WHERE stock_id = ANY(%s) AND timestamp >= %s AND timestamp < %s
            ORDER BY stock_id, timestamp ASC
            """,
            (candidate_ids, start_inclusive, end_exclusive),
        )
    candle_rows, daily_quality = normalize_daily_candles(candle_rows)
    if daily_quality["duplicate_sessions"]:
        logger.info(
            "Collapsed {} duplicate daily session(s) before enhanced momentum replay",
            daily_quality["duplicate_sessions"],
        )
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily session(s) before enhanced momentum replay",
            daily_quality["skipped_ambiguous_sessions"],
        )
    report_progress(59, "Preparing candle history", len(candle_rows), len(candle_rows), "candles", f"Loaded {len(candle_rows):,} daily candles")

    candles_by_stock: dict[int, list[dict[str, Any]]] = {}
    for candle in candle_rows:
        candles_by_stock.setdefault(int(candle["stock_id"]), []).append(dict(candle))

    scanner_stocks: list[dict[str, Any]] = []
    candidate_items = list(candidates_by_stock.items())
    for stock_index, (stock_id, stock_candidates) in enumerate(candidate_items, start=1):
        report_progress(
            60 + 14 * (stock_index - 1) / max(len(candidate_items), 1),
            "Replaying scanner trades",
            stock_index - 1,
            len(candidate_items),
            "stocks",
            f"Replaying {stock_candidates[-1].get('symbol', stock_id)}",
        )
        stock_candidates.sort(key=lambda row: _enhanced_backtest_date(row.get("as_of_date")) or date.max)
        stock_result = _enhanced_backtest_replay_stock(
            stock_candidates,
            candles_by_stock.get(stock_id, []),
            signal_cutoff,
            end_date,
            request.target_atr,
            request.stop_atr,
            signal_generation_capital,
            charge_config,
        )
        scanner_stocks.append(stock_result)
        report_progress(
            60 + 14 * stock_index / max(len(candidate_items), 1),
            "Replaying scanner trades",
            stock_index,
            len(candidate_items),
            "stocks",
            f"Finished {stock_result.get('symbol', stock_id)}",
        )
    scanner_stocks, scanner_strategy_summary = _enhanced_backtest_portfolio_replay(
        scanner_stocks,
        candidates_by_stock,
        rank_by_stock_snapshot,
        slot_count=slot_count,
        capital_per_slot=request.capital_per_stock,
        charge_config=charge_config,
        end_date=end_date,
    )
    strategies: list[dict[str, Any]] = [{
        "strategy_id": "enhanced_momentum",
        "strategy_name": "Enhanced Momentum scanner",
        "strategy_type": "scanner",
        "description": f"Enhanced Momentum refreshes {_enhanced_momentum_refresh_label(request.refresh_frequency)}. Each setup keeps its own exit rules across refreshes; up to {slot_count} shared capital slots are reused when trades close.",
        "timeframe": "1d",
        "risk_rules": {"target_atr": request.target_atr, "stop_atr": request.stop_atr, "capital_slots": slot_count},
        "summary": scanner_strategy_summary,
        "stocks": scanner_stocks,
    }]

    if request.include_top_n_buy_hold:
        raw_buy_hold_stocks: list[dict[str, Any]] = []
        for cohort_index, batch in enumerate(cohort_batches):
            batch_end = cohort_batches[cohort_index + 1]["date"] if cohort_index + 1 < len(cohort_batches) else end_date
            for candidate in batch["candidates"]:
                raw_buy_hold_stocks.append(
                    _enhanced_backtest_buy_hold_stock(
                        candidate,
                        candles_by_stock.get(int(candidate["stock_id"]), []),
                        batch["date"],
                        batch_end,
                        signal_generation_capital,
                        charge_config,
                    )
                )
        buy_hold_stocks, buy_hold_summary = _enhanced_backtest_portfolio_replay(
            raw_buy_hold_stocks,
            candidates_by_stock,
            rank_by_stock_snapshot,
            slot_count=slot_count,
            capital_per_slot=request.capital_per_stock,
            charge_config=charge_config,
            end_date=end_date,
        )
        buy_hold_summary.update({
            "scanner_eligible_stocks": fresh_snapshot_match_count,
            "selected_top_n": slot_count,
            "requested_top_n": request.filters.top_n,
            "stocks_skipped": sum(stock.get("status") == "data_unavailable" for stock in buy_hold_stocks),
        })
        selection_label = (
            f"top {request.filters.top_n} ranked stocks"
            if request.filters.top_n is not None
            else "all matching stocks"
        )
        strategies.append({
            "strategy_id": "enhanced_momentum_top_n_buy_hold",
            "strategy_name": f"Enhanced Momentum buy & hold · {selection_label}",
            "strategy_type": "buy_and_hold",
            "description": (
                f"Buys {selection_label} at the next trading-session open after each refreshed cohort, then sells at the next refresh close. "
                "Any available capital slot is reused for the next eligible cohort; the final cohort exits at the backtest end date."
            ),
            "timeframe": "1d",
            "risk_rules": {"selected_top_n": slot_count, "refresh_frequency": request.refresh_frequency, "capital_slots": slot_count},
            "summary": buy_hold_summary,
            "stocks": buy_hold_stocks,
        })

    preset_requirements: dict[str, dict[str, Any]] = {}
    overlay_total = len(candidate_items) * (len(selected_presets) + len(fundamental_setup_ids))
    overlay_completed = 0

    def report_overlay_progress(detail: str) -> None:
        nonlocal overlay_completed
        overlay_completed += 1
        progress = 77 + 21 * overlay_completed / max(overlay_total, 1)
        report_progress(
            progress,
            "Evaluating selected replay setups",
            overlay_completed,
            overlay_total,
            "stock/filter checks",
            detail,
        )

    if selected_presets or fundamental_setup_ids:
        report_progress(
            75,
            "Preparing selected replay setups",
            0,
            overlay_total,
            "stock/filter checks",
            f"Preparing {len(selected_presets)} filters and {len(fundamental_setup_ids)} standalone setups",
        )
        aggregate_requirements: dict[str, dict[str, Any]] = {}
        for preset in selected_presets:
            _, requirements, errors = _parse_filter_expression(
                FilterRequirementsRequest(
                    mode=EvaluationMode.BACKTEST,
                    expression=preset.expression.model_dump(mode="json"),
                )
            )
            if errors:
                raise HTTPException(status_code=422, detail={"preset_id": preset.preset_id, "errors": errors})
            preset_requirements[preset.preset_id] = requirements
            for timeframe, current in requirements.items():
                existing = aggregate_requirements.setdefault(
                    timeframe,
                    {"minimum_candles": 0, "warmup": 0, "required_fields": [], "requires_live": False},
                )
                existing["minimum_candles"] = max(existing["minimum_candles"], current["minimum_candles"])
                existing["warmup"] = max(existing["warmup"], current["warmup"])
                existing["required_fields"] = sorted(set(existing["required_fields"]) | set(current["required_fields"]))
                existing["requires_live"] = existing["requires_live"] or current["requires_live"]
            aggregate_requirements.setdefault(preset.timeframe.value, {})

        profile_strategies: dict[str, list[dict[str, Any]]] = {preset.preset_id: [] for preset in selected_presets}
        profile_errors: dict[str, int] = {preset.preset_id: 0 for preset in selected_presets}
        setup_stock_results: dict[str, list[dict[str, Any]]] = {setup_id: [] for setup_id in fundamental_setup_ids}
        fundamental_setup_errors: dict[str, int] = {setup_id: 0 for setup_id in fundamental_setup_ids}
        eligible_batches: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        for stock_id, stock_candidates in candidates_by_stock.items():
            entry_signal_date_map = profile_gates.get(stock_id) or {}
            representative = dict(stock_candidates[-1])
            representative["scanner_signal_count"] = len(stock_candidates)
            if not entry_signal_date_map:
                for preset in selected_presets:
                    profile_strategies[preset.preset_id].append(
                        _enhanced_backtest_idle_stock(representative, request.capital_per_stock, "no_next_session")
                    )
                    report_overlay_progress(
                        f"{representative.get('symbol')} · {preset.name} (no eligible next session)"
                    )
                for setup_id in fundamental_setup_ids:
                    setup_stock_results[setup_id].append(
                        _enhanced_backtest_idle_stock(representative, request.capital_per_stock, "no_next_session")
                    )
                    report_overlay_progress(
                        f"{representative.get('symbol')} · {SETUPS[setup_id]['name']} (no eligible next session)"
                    )
                continue
            eligible_batches.append((stock_id, representative, entry_signal_date_map))

        if eligible_batches:
            overlay_workers = min(_MAX_ENHANCED_MOMENTUM_BACKTEST_WORKERS, len(eligible_batches))
            report_progress(
                77 + 21 * overlay_completed / max(overlay_total, 1),
                "Evaluating selected replay setups",
                overlay_completed,
                overlay_total,
                "stock/filter checks",
                f"Evaluating {len(eligible_batches)} stocks with {overlay_workers} parallel workers",
            )
            with ProcessPoolExecutor(
                max_workers=overlay_workers,
                mp_context=get_context("spawn"),
                initializer=_initialize_cpu_analysis_worker,
            ) as executor:
                futures = {
                    executor.submit(
                        _enhanced_backtest_selected_stock_batch,
                        representative,
                        preset_ids,
                        signal_cutoff.isoformat(),
                        end_date.isoformat(),
                        signal_generation_capital,
                        aggregate_requirements,
                        preset_requirements,
                        charge_config.to_dict(),
                        entry_signal_date_map,
                        fundamental_setup_ids,
                    ): (stock_id, representative)
                    for stock_id, representative, entry_signal_date_map in eligible_batches
                }
                for future in as_completed(futures):
                    stock_id, representative = futures[future]
                    try:
                        batch_result = future.result()
                    except Exception as error:
                        logger.warning(
                            "Enhanced momentum overlay batch skipped {}: {}",
                            representative.get("symbol"),
                            error,
                        )
                        batch_result = {
                            "results": {
                                preset.preset_id: _enhanced_backtest_idle_stock(
                                    representative,
                                    request.capital_per_stock,
                                    "data_unavailable",
                                )
                                for preset in selected_presets
                            },
                            "errors": {preset.preset_id: str(error) for preset in selected_presets},
                            "fundamental_setup_results": {
                                setup_id: _enhanced_backtest_idle_stock(
                                    representative,
                                    request.capital_per_stock,
                                    "data_unavailable",
                                )
                                for setup_id in fundamental_setup_ids
                            },
                            "fundamental_setup_errors": {
                                setup_id: str(error) for setup_id in fundamental_setup_ids
                            },
                        }

                    for preset in selected_presets:
                        profile_result = batch_result.get("results", {}).get(preset.preset_id)
                        if profile_result is None:
                            profile_result = _enhanced_backtest_idle_stock(
                                representative,
                                request.capital_per_stock,
                                "data_unavailable",
                            )
                            batch_result.setdefault("errors", {})[preset.preset_id] = "Worker returned no stock result"
                        error_detail = batch_result.get("errors", {}).get(preset.preset_id)
                        if error_detail:
                            logger.warning(
                                "Enhanced momentum overlay {} skipped {}: {}",
                                preset.preset_id,
                                representative.get("symbol"),
                                error_detail,
                            )
                            profile_errors[preset.preset_id] += 1
                        profile_strategies[preset.preset_id].append(profile_result)
                        suffix = " (data unavailable)" if error_detail else ""
                        report_overlay_progress(f"{representative.get('symbol')} · {preset.name}{suffix}")

                    for setup_id in fundamental_setup_ids:
                        setup_result = batch_result.get("fundamental_setup_results", {}).get(setup_id)
                        if setup_result is None:
                            setup_result = _enhanced_backtest_idle_stock(
                                representative,
                                request.capital_per_stock,
                                "data_unavailable",
                            )
                            batch_result.setdefault("fundamental_setup_errors", {})[setup_id] = (
                                "Worker returned no setup result"
                            )
                        error_detail = batch_result.get("fundamental_setup_errors", {}).get(setup_id)
                        if error_detail:
                            logger.warning(
                                "Enhanced momentum Fundamental Setup {} skipped {}: {}",
                                setup_id,
                                representative.get("symbol"),
                                error_detail,
                            )
                            fundamental_setup_errors[setup_id] += 1
                        setup_stock_results[setup_id].append(setup_result)
                        suffix = " (data unavailable)" if error_detail else ""
                        report_overlay_progress(
                            f"{representative.get('symbol')} · {SETUPS[setup_id]['name']}{suffix}"
                        )

        for preset in selected_presets:
            profile_stocks, profile_summary = _enhanced_backtest_portfolio_replay(
                profile_strategies[preset.preset_id],
                candidates_by_stock,
                rank_by_stock_snapshot,
                slot_count=slot_count,
                capital_per_slot=request.capital_per_stock,
                charge_config=charge_config,
                end_date=end_date,
            )
            strategies.append({
                "strategy_id": preset.preset_id,
                "strategy_name": preset.name,
                "strategy_type": "watchlist_analysis_preset",
                "description": f"{preset.description} The scanner cohort refreshes {_enhanced_momentum_refresh_label(request.refresh_frequency)}; open positions keep this setup's exit rules and capital slot until they close.",
                "timeframe": preset.timeframe.value,
                "risk_rules": {
                    "stop_loss_pct": preset.default_stop_loss_pct,
                    "take_profit_pct": preset.default_take_profit_pct,
                    "max_hold_bars": preset.max_hold_bars,
                    "capital_slots": slot_count,
                    "refresh_frequency": request.refresh_frequency,
                },
                "summary": {
                    **profile_summary,
                    "scanner_eligible_stocks": len(candidates_by_stock),
                    "stocks_skipped": profile_errors[preset.preset_id],
                },
                "stocks": profile_stocks,
            })

        for setup_id in fundamental_setup_ids:
            setup_stocks, setup_summary = _enhanced_backtest_portfolio_replay(
                setup_stock_results[setup_id],
                candidates_by_stock,
                rank_by_stock_snapshot,
                slot_count=slot_count,
                capital_per_slot=request.capital_per_stock,
                charge_config=charge_config,
                end_date=end_date,
            )
            setup = SETUPS[setup_id]
            strategies.append({
                "strategy_id": setup_id,
                "strategy_name": setup["name"],
                "strategy_type": "fundamental_setup",
                "description": (
                    f"{setup['description']} Evaluated on five-minute candles while each stock is in an Enhanced Momentum cohort; "
                    f"the cohort refreshes {_enhanced_momentum_refresh_label(request.refresh_frequency)}. "
                    "Entries use the next five-minute candle open; a 2R target, 0.5% stop buffer, and 1,500-bar time exit apply."
                ),
                "timeframe": FUNDAMENTAL_SETUP_CANDLE_INTERVAL,
                "risk_rules": {
                    "target_r": 2.0,
                    "max_hold_bars": 1_500,
                    "stop_buffer_pct": 0.5,
                    "slippage_pct": 0.05,
                    "capital_slots": slot_count,
                    "refresh_frequency": request.refresh_frequency,
                },
                "summary": {
                    **setup_summary,
                    "scanner_eligible_stocks": len(candidates_by_stock),
                    "stocks_skipped": fundamental_setup_errors[setup_id],
                },
                "stocks": setup_stocks,
            })

    base_summary = scanner_strategy_summary
    summary = {
        "matching_stocks": matching_stock_sessions,
        "returned_stocks": len(candidates_by_stock),
        "scanner_snapshot_returned_stocks": snapshot_returned_stocks,
        "stale_snapshot_results_excluded": stale_snapshot_results_excluded,
        "entry_candidates": scanner_signal_count,
        "executed_trades": int(base_summary["total_trades"]),
        "completed_forward_windows": int(base_summary["stocks_traded"]),
        "partial_forward_windows": max(0, len(candidates_by_stock) - int(base_summary["stocks_traded"])),
        "average_horizon_return_pct": base_summary["average_stock_return_pct"],
        "median_horizon_return_pct": base_summary["median_stock_return_pct"],
        "positive_horizon_trades": int(base_summary["positive_stocks"]),
        "horizon_win_rate_pct": base_summary["positive_stock_rate_pct"],
        "target_eligible_trades": base_summary["target_eligible_trades"],
        "target_hits": base_summary["target_hits"],
        "target_hit_rate_pct": base_summary["target_hit_rate_pct"],
        "stop_hits": base_summary["stop_hits"],
        "average_net_trade_return_pct": None,
        "aggregate_net_pnl": base_summary["net_pnl"],
        "aggregate_invested_capital": base_summary["starting_capital"],
        "aggregate_net_return_pct": base_summary["total_return_pct"],
        "average_stock_return_pct": base_summary["average_stock_return_pct"],
        "total_final_capital": base_summary["final_capital"],
        "scanner_sessions": len(session_summaries),
        "scanner_signal_count": scanner_signal_count,
        "unique_scanner_stocks": len(candidates_by_stock),
        "refresh_frequency": request.refresh_frequency,
        "capital_slots": slot_count,
        "maximum_concurrent_positions": base_summary["maximum_concurrent_positions"],
        "blocked_trade_signals": base_summary["blocked_trade_signals"],
        "strategy_count": len(strategies),
    }
    scan_details = {
        "scanner": "enhanced_swing_momentum",
        "cohort_mode": "scheduled_refresh",
        "strategy_mode": "filter_replay",
        "cohort_date": cohort_date.isoformat() if cohort_date else None,
        "scan_start_date": cohort_date.isoformat() if cohort_date else signal_cutoff.isoformat(),
        "scan_end_date": cohort_dates[-1].isoformat() if cohort_dates else None,
        "replay_start_date": signal_cutoff.isoformat(),
        "replay_end_date": end_date.isoformat(),
        "refresh_frequency": request.refresh_frequency,
        "refresh_dates": [item.isoformat() for item in cohort_dates],
        "session_dates": [item.isoformat() for item in cohort_dates],
        "replay_session_count": len(session_dates),
        "matching_stock_sessions": matching_stock_sessions,
        "snapshot_matching_stocks": matching_stock_sessions,
        "snapshot_returned_stocks": snapshot_returned_stocks,
        "fresh_snapshot_stocks": fresh_snapshot_match_count,
        "selected_ranked_stocks": scanner_signal_count,
        "maximum_cohort_size": max_cohort_size,
        "capital_slots": slot_count,
        "starting_portfolio_capital": round(slot_count * request.capital_per_stock, 2),
        "top_n_requested": request.filters.top_n,
        "stale_snapshot_results_excluded": stale_snapshot_results_excluded,
        "top_n_selection_behavior": "When Top N is set, the highest-ranked fresh stocks are selected after stale snapshot rows are excluded; otherwise every fresh match is included.",
        "unique_scanner_stocks": len(candidates_by_stock),
        "cohort_sizes": [{"date": batch["date"].isoformat(), "selected_stocks": len(batch["candidates"])} for batch in cohort_batches],
        "cohort_freshness_rule": "Only scanner results with a daily candle dated exactly on the snapshot date enter the cohort; returned rows with older candles are excluded as stale.",
        "profile_entry_rule": "Each ranked cohort can trigger selected Watchlist Analysis presets and standalone Fundamental Setups from the session after its scan through the next scan session. A position already open at refresh remains with its setup and exits by its own rules.",
        "capital_allocation_rule": "Each strategy has a shared pool of capital slots equal to Top N when set; if Top N is blank, the pool uses the largest selected cohort in the replay. At most one open position occupies a slot; after its trade exits, that slot can fund a later eligible trade. Signals are not filled retroactively while every slot is occupied.",
        "buy_hold_exit_rule": "Buy-and-hold positions exit at the next scheduled scanner refresh close; the final cohort exits at the backtest end date.",
        "selected_preset_ids": preset_ids,
        "selected_fundamental_setup_ids": fundamental_setup_ids,
        "selected_builtin_filters": ["top_n_buy_hold"] if request.include_top_n_buy_hold else [],
        "latest_market_context": (latest_scan or {}).get("market_context", {}),
        "formula": (latest_scan or {}).get("formula", {}),
        "sessions": session_summaries,
    }
    report_progress(99, "Saving results", len(strategies), len(strategies), "strategies", "Saving the completed replay")
    run = _save_enhanced_backtest_run(
        db,
        (
            signal_cutoff,
            end_date,
            stored_months,
            _json.dumps({
                **historical_request.model_dump(mode="json"),
                "preset_ids": preset_ids,
                "fundamental_setup_ids": fundamental_setup_ids,
                "include_top_n_buy_hold": request.include_top_n_buy_hold,
            }),
            _json.dumps({
                "strategy_mode": "filter_replay",
                "target_atr": request.target_atr,
                "stop_atr": request.stop_atr,
                "capital_per_stock": request.capital_per_stock,
                "include_top_n_buy_hold": request.include_top_n_buy_hold,
                "refresh_frequency": request.refresh_frequency,
                "capital_slots": slot_count,
                "charge_config": charge_config.to_dict(),
                "preset_rules": [
                    {
                        "preset_id": preset.preset_id,
                        "name": preset.name,
                        "timeframe": preset.timeframe.value,
                        "stop_loss_pct": preset.default_stop_loss_pct,
                        "take_profit_pct": preset.default_take_profit_pct,
                        "max_hold_bars": preset.max_hold_bars,
                    }
                    for preset in selected_presets
                ],
                "fundamental_setup_rules": [
                    {
                        "setup_id": setup_id,
                        "name": SETUPS[setup_id]["name"],
                        "timeframe": FUNDAMENTAL_SETUP_CANDLE_INTERVAL,
                        "target_r": 2.0,
                        "max_hold_bars": 1_500,
                        "stop_buffer_pct": 0.5,
                        "slippage_pct": 0.05,
                    }
                    for setup_id in fundamental_setup_ids
                ],
            }),
            _json.dumps(summary),
            _json.dumps(scan_details),
            _json.dumps([]),
            _json.dumps(strategies),
        ),
        job_id=job_id,
    )
    return {
        "run": {
            "id": int(run["id"]),
            "signal_cutoff_date": signal_cutoff.isoformat(),
            "end_date": end_date.isoformat(),
            "lookback_months": stored_months,
            "filters": historical_request.model_dump(mode="json"),
            "preset_ids": preset_ids,
            "fundamental_setup_ids": fundamental_setup_ids,
            "config": {
                "strategy_mode": "filter_replay",
                "target_atr": request.target_atr,
                "stop_atr": request.stop_atr,
                "capital_per_stock": request.capital_per_stock,
                "include_top_n_buy_hold": request.include_top_n_buy_hold,
                "refresh_frequency": request.refresh_frequency,
                "capital_slots": slot_count,
                "charge_config": charge_config.to_dict(),
            },
            "summary": summary,
            "scan": scan_details,
            "results": [],
            "strategies": strategies,
            "created_at": _serialize_row({"created_at": run.get("created_at")})["created_at"],
        }
    }


def _execute_enhanced_momentum_buy_hold_backtest(
    request: EnhancedMomentumBacktestRequest,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Rank the start-date scanner matches, buy top N, and sell at period end."""
    import json as _json

    def report_progress(percent: float, phase: str, completed: int, total: int, unit: str, detail: str) -> None:
        if progress_callback is not None:
            progress_callback({
                "percent": round(max(0.0, min(percent, 99.0)), 1),
                "phase": phase,
                "completed": completed,
                "total": total,
                "unit": unit,
                "detail": detail,
            })

    signal_cutoff, end_date, stored_months = _enhanced_backtest_window(request)
    db = get_db()
    historical_filters = request.filters.model_dump(exclude={"as_of_date"})
    start_inclusive = datetime.combine(signal_cutoff, datetime.min.time(), tzinfo=IST)
    end_exclusive = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    session_rows = db.fetch_all(
        """
        SELECT DISTINCT (timestamp AT TIME ZONE 'Asia/Kolkata')::date AS session_date
        FROM candles_1d
        WHERE timestamp >= %s AND timestamp < %s
        ORDER BY session_date
        """,
        (start_inclusive, end_exclusive),
    )
    session_dates = [
        parsed
        for row in session_rows
        if (parsed := _enhanced_backtest_date(row.get("session_date"))) is not None
    ]
    cohort_date = session_dates[0] if session_dates else None
    report_progress(3, "Preparing scanner cohort", 0, 1 if cohort_date else 0, "snapshot", f"Found {len(session_dates)} sessions in the explicit date range")

    matching_stocks = 0
    snapshot_returned_stocks = 0
    stale_snapshot_results_excluded = 0
    session_summaries: list[dict[str, Any]] = []
    latest_scan: dict[str, Any] | None = None
    fresh_candidates: list[dict[str, Any]] = []
    if cohort_date is not None:
        report_progress(5, "Scanning cohort snapshot", 0, 1, "snapshot", f"Ranking Enhanced Momentum stocks as of {cohort_date.isoformat()}")
        latest_scan = _enhanced_momentum_scan_session_worker(cohort_date, historical_filters)
        matching_stocks = int(latest_scan.get("matching_stocks") or 0)
        scanner_results = latest_scan.get("results") or []
        snapshot_returned_stocks = int(
            latest_scan.get("returned_stocks")
            if latest_scan.get("returned_stocks") is not None
            else len(scanner_results)
        )
        session_summaries.append({
            "date": cohort_date.isoformat(),
            "matching_stocks": matching_stocks,
            "returned_stocks": snapshot_returned_stocks,
            "market_regime": (latest_scan.get("market_context") or {}).get("regime"),
        })
        for candidate in scanner_results:
            if _enhanced_backtest_date(candidate.get("as_of_date")) != cohort_date:
                stale_snapshot_results_excluded += 1
                continue
            fresh_candidates.append(dict(candidate))

    fresh_candidates.sort(key=lambda row: (int(row.get("rank") or 10**9), -(float(row.get("enhanced_score") or 0))))
    buy_hold_limit = request.filters.top_n if request.filters.top_n is not None else request.top_n
    selected_candidates = fresh_candidates[: buy_hold_limit]
    selected_by_stock = {int(candidate["stock_id"]): candidate for candidate in selected_candidates}
    report_progress(
        50,
        "Top stocks selected",
        len(selected_candidates),
        buy_hold_limit,
        "stocks",
        f"Selected {len(selected_candidates)} of {len(fresh_candidates)} dated scanner matches",
    )

    charge_config = config_from_store(store("config"))
    selected_ids = list(selected_by_stock)
    candle_rows: list[dict[str, Any]] = []
    if selected_ids:
        candle_rows = db.fetch_all(
            """
            SELECT stock_id, timestamp, open, high, low, close
            FROM candles_1d
            WHERE stock_id = ANY(%s) AND timestamp >= %s AND timestamp < %s
            ORDER BY stock_id, timestamp ASC
            """,
            (selected_ids, start_inclusive, end_exclusive),
        )
    candle_rows, daily_quality = normalize_daily_candles(candle_rows)
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily session(s) before buy-and-hold replay",
            daily_quality["skipped_ambiguous_sessions"],
        )
    candles_by_stock: dict[int, list[dict[str, Any]]] = {}
    for candle in candle_rows:
        candles_by_stock.setdefault(int(candle["stock_id"]), []).append(dict(candle))
    report_progress(60, "Preparing daily prices", len(candle_rows), len(candle_rows), "candles", f"Loaded {len(candle_rows):,} daily candles for the selected stocks")

    stocks: list[dict[str, Any]] = []
    selected_items = list(selected_by_stock.items())
    for index, (stock_id, candidate) in enumerate(selected_items, start=1):
        stocks.append(_enhanced_backtest_buy_hold_stock(
            candidate,
            candles_by_stock.get(stock_id, []),
            cohort_date or signal_cutoff,
            end_date,
            request.capital_per_stock,
            charge_config,
        ))
        report_progress(
            60 + 35 * index / max(len(selected_items), 1),
            "Buying and holding selected stocks",
            index,
            len(selected_items),
            "stocks",
            f"Completed {candidate.get('symbol', stock_id)}",
        )

    stocks.sort(key=lambda row: (row.get("return_pct") is None, -(row.get("return_pct") or 0), row.get("symbol") or ""))
    strategy_summary = _enhanced_backtest_strategy_summary(stocks)
    strategy_summary["scanner_eligible_stocks"] = len(fresh_candidates)
    strategy_summary["selected_top_n"] = len(selected_candidates)
    strategy_summary["stocks_skipped"] = sum(stock.get("status") == "data_unavailable" for stock in stocks)
    description = (
        f"Ranks every Enhanced Momentum match on {cohort_date.isoformat() if cohort_date else signal_cutoff.isoformat()}, "
        f"buys the top {buy_hold_limit} at the next session open, and sells at the last available close on or before "
        f"{end_date.isoformat()}. No Watchlist Analysis setup, stop, or target rules are applied."
    )
    strategies = [{
        "strategy_id": "enhanced_momentum_top_n_buy_hold",
        "strategy_name": f"Enhanced Momentum top {buy_hold_limit} buy & hold",
        "strategy_type": "buy_and_hold",
        "description": description,
        "timeframe": "1d",
        "risk_rules": {"top_n": buy_hold_limit},
        "summary": strategy_summary,
        "stocks": stocks,
    }]

    summary = {
        "matching_stocks": matching_stocks,
        "returned_stocks": len(selected_candidates),
        "scanner_snapshot_returned_stocks": snapshot_returned_stocks,
        "stale_snapshot_results_excluded": stale_snapshot_results_excluded,
        "entry_candidates": len(selected_candidates),
        "executed_trades": int(strategy_summary["total_trades"]),
        "completed_forward_windows": int(strategy_summary["stocks_traded"]),
        "partial_forward_windows": max(0, len(selected_candidates) - int(strategy_summary["stocks_traded"])),
        "average_horizon_return_pct": strategy_summary["average_stock_return_pct"],
        "median_horizon_return_pct": strategy_summary["median_stock_return_pct"],
        "positive_horizon_trades": int(strategy_summary["positive_stocks"]),
        "horizon_win_rate_pct": strategy_summary["positive_stock_rate_pct"],
        "target_eligible_trades": 0,
        "target_hits": 0,
        "target_hit_rate_pct": None,
        "stop_hits": 0,
        "average_net_trade_return_pct": None,
        "aggregate_net_pnl": strategy_summary["net_pnl"],
        "aggregate_invested_capital": strategy_summary["starting_capital"],
        "aggregate_net_return_pct": strategy_summary["total_return_pct"],
        "average_stock_return_pct": strategy_summary["average_stock_return_pct"],
        "total_final_capital": strategy_summary["final_capital"],
        "scanner_sessions": len(session_summaries),
        "scanner_signal_count": len(selected_candidates),
        "unique_scanner_stocks": len(selected_candidates),
        "strategy_count": 1,
    }
    historical_request = EnhancedMomentumScanRequest(
        **historical_filters,
        as_of_date=cohort_date or signal_cutoff,
    )
    scan_details = {
        "scanner": "enhanced_swing_momentum",
        "cohort_mode": "fixed_snapshot",
        "strategy_mode": "buy_and_hold",
        "cohort_date": cohort_date.isoformat() if cohort_date else None,
        "scan_start_date": cohort_date.isoformat() if cohort_date else signal_cutoff.isoformat(),
        "scan_end_date": cohort_date.isoformat() if cohort_date else None,
        "replay_start_date": signal_cutoff.isoformat(),
        "replay_end_date": end_date.isoformat(),
        "replay_session_count": len(session_dates),
        "matching_stock_sessions": matching_stocks,
        "snapshot_matching_stocks": matching_stocks,
        "snapshot_returned_stocks": snapshot_returned_stocks,
        "fresh_snapshot_stocks": len(fresh_candidates),
        "selected_top_n": len(selected_candidates),
        "top_n_requested": buy_hold_limit,
        "unique_scanner_stocks": len(selected_candidates),
        "stale_snapshot_results_excluded": stale_snapshot_results_excluded,
        "result_limit_behavior": "The scanner display limit is ignored; every matching stock is ranked before selecting the requested top N.",
        "entry_exit_rule": "The scan is point-in-time at the first session on or after From. Buy at the following session open; sell at the last available session close on or before To.",
        "selected_preset_ids": [],
        "latest_market_context": (latest_scan or {}).get("market_context", {}),
        "formula": (latest_scan or {}).get("formula", {}),
        "sessions": session_summaries,
    }
    report_progress(98, "Saving results", 1, 1, "strategy", "Saving the buy-and-hold result")
    run = _save_enhanced_backtest_run(
        db,
        (
            signal_cutoff,
            end_date,
            stored_months,
            _json.dumps({**historical_request.model_dump(mode="json"), "preset_ids": [], "strategy_mode": "buy_and_hold", "top_n": buy_hold_limit}),
            _json.dumps({
                "strategy_mode": "buy_and_hold",
                "top_n": buy_hold_limit,
                "capital_per_stock": request.capital_per_stock,
                "charge_config": charge_config.to_dict(),
            }),
            _json.dumps(summary),
            _json.dumps(scan_details),
            _json.dumps([]),
            _json.dumps(strategies),
        ),
        job_id=job_id,
    )
    return {"run": {
        "id": int(run["id"]),
        "signal_cutoff_date": signal_cutoff.isoformat(),
        "end_date": end_date.isoformat(),
        "lookback_months": stored_months,
        "filters": historical_request.model_dump(mode="json"),
        "preset_ids": [],
        "config": {
            "strategy_mode": "buy_and_hold",
            "top_n": buy_hold_limit,
            "capital_per_stock": request.capital_per_stock,
            "charge_config": charge_config.to_dict(),
        },
        "summary": summary,
        "scan": scan_details,
        "results": [],
        "strategies": strategies,
        "created_at": _serialize_row({"created_at": run.get("created_at")})["created_at"],
    }}


_ENHANCED_BACKTEST_JOB_RESPONSE_KEYS = (
    "job_id", "submission_id", "status", "created_at", "started_at", "completed_at", "progress", "run_id", "error",
)
_ENHANCED_BACKTEST_JOB_UPDATE_COLUMNS = frozenset({
    "status", "progress_json", "run_id", "error", "worker_id", "heartbeat_at", "started_at", "completed_at",
})


def _enhanced_backtest_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """Copy a replay job into a JSON-safe response without exposing mutability.

    Only the public handle fields are returned; worker ownership and heartbeat
    bookkeeping stay internal so the response shape is unchanged.
    """
    return _serialize_row({
        key: value.copy() if isinstance(value, dict) else value
        for key, value in job.items()
        if key in _ENHANCED_BACKTEST_JOB_RESPONSE_KEYS
    })


def _decode_enhanced_backtest_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if value is not None else {}


def _enhanced_backtest_request_fingerprint(request: EnhancedMomentumBacktestRequest) -> str:
    """Stable digest of everything that affects the replay, excluding the identity."""
    payload = request.model_dump(mode="json", exclude={"submission_id"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _admit_enhanced_backtest_job(job: dict[str, Any], request: EnhancedMomentumBacktestRequest) -> str:
    """Record a new job and its request, atomically, before any worker starts.

    Admission is one statement: the insert only happens while no other job
    is active, and the partial unique index closes the remaining window in
    which two processes pass that guard at the same time. Returns
    'admitted', 'busy' (another replay holds the slot) or 'duplicate' (the
    submission identity was already recorded by a concurrent retry). A
    storage failure propagates: without a saved job there is nothing a
    restarted API could answer for, so the caller must not get a handle.
    """
    params = {
        "job_id": job["job_id"],
        "submission_id": job.get("submission_id"),
        "request_fingerprint": job.get("request_fingerprint"),
        "status": job["status"],
        "request_json": json.dumps(request.model_dump(mode="json")),
        "progress_json": json.dumps(job.get("progress") or {}),
        "run_id": job.get("run_id"),
        "error": job.get("error"),
        "worker_id": _ENHANCED_BACKTEST_WORKER_ID,
        "heartbeat_at": job["created_at"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
    }
    try:
        admitted = get_db().execute_returning(
            """
            INSERT INTO enhanced_momentum_backtest_jobs
                (job_id, submission_id, request_fingerprint, status, request_json, progress_json,
                 run_id, error, worker_id, heartbeat_at, created_at, started_at, completed_at)
            SELECT %(job_id)s, %(submission_id)s, %(request_fingerprint)s, %(status)s, %(request_json)s,
                   %(progress_json)s, %(run_id)s, %(error)s, %(worker_id)s, %(heartbeat_at)s,
                   %(created_at)s, %(started_at)s, %(completed_at)s
            WHERE NOT EXISTS (
                SELECT 1 FROM enhanced_momentum_backtest_jobs WHERE status IN ('queued', 'running')
            )
            RETURNING job_id
            """,
            params,
        )
    except DatabaseError as error:
        if _ENHANCED_BACKTEST_SUBMISSION_INDEX in str(error):
            return "duplicate"
        if _ENHANCED_BACKTEST_SINGLE_ACTIVE_INDEX in str(error):
            return "busy"
        raise
    return "admitted" if admitted is not None else "busy"


def _load_enhanced_backtest_job_by_submission(submission_id: str) -> dict[str, Any] | None:
    row = get_db().fetch_one(
        _ENHANCED_BACKTEST_JOB_SELECT + " WHERE submission_id = %(submission_id)s",
        {"submission_id": submission_id},
    )
    return _enhanced_backtest_job_from_row(row) if row else None


def _claim_enhanced_backtest_job(job_id: str) -> bool:
    """Take back a queued job this process admitted but never started.

    That happens when admission committed but its reply was lost before the
    worker thread was launched. The claim is conditional in storage, so a
    job that was meanwhile settled (or is already running) is not touched.
    """
    return _write_enhanced_backtest_job(
        job_id,
        {"heartbeat_at": datetime.now(timezone.utc)},
        " WHERE job_id = %(job_id)s AND worker_id = %(owner_id)s AND status = 'queued'",
        {"owner_id": _ENHANCED_BACKTEST_WORKER_ID},
    ) == "written"


_ENHANCED_BACKTEST_WRITE_FENCE = (
    " WHERE job_id = %(job_id)s AND worker_id = %(owner_id)s AND status IN ('queued', 'running')"
)


def _enhanced_backtest_job_assignments(changes: dict[str, Any]) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    for key, value in changes.items():
        column = "progress_json" if key == "progress" else key
        if column not in _ENHANCED_BACKTEST_JOB_UPDATE_COLUMNS or column == "worker_id":
            continue
        columns[column] = json.dumps(value) if column == "progress_json" else value
    return columns


def _write_enhanced_backtest_job(job_id: str, changes: dict[str, Any], predicate: str, params: dict[str, Any]) -> str:
    """Conditionally update a job row. Returns 'written', 'rejected' or 'unavailable'."""
    columns = _enhanced_backtest_job_assignments(changes)
    if not columns:
        return "written"
    assignments = ", ".join(f"{column} = %({column})s" for column in columns)
    try:
        affected = get_db().execute(
            f"UPDATE enhanced_momentum_backtest_jobs SET {assignments}{predicate}",
            {**columns, **params, "job_id": job_id},
        )
    except Exception as error:  # noqa: BLE001 - a storage blip is not a loss of ownership
        logger.warning("Could not persist enhanced momentum replay job {} update: {}", job_id, error)
        return "unavailable"
    return "written" if affected else "rejected"


def _persist_enhanced_backtest_job_changes(job_id: str, changes: dict[str, Any]) -> str:
    """Owner-fenced write of progress, heartbeat or outcome.

    The row is only touched while it is still active and still owned by this
    worker. 'rejected' means the job was settled or replaced elsewhere and
    this worker must not write anything further. Storage errors are reported
    as 'unavailable' rather than raised so a blip cannot abort a replay; a
    lost terminal write stays recoverable because the saved run carries the
    job id.
    """
    return _write_enhanced_backtest_job(
        job_id,
        changes,
        _ENHANCED_BACKTEST_WRITE_FENCE,
        {"owner_id": _ENHANCED_BACKTEST_WORKER_ID},
    )


def _forfeit_enhanced_backtest_job(job_id: str) -> None:
    """Drop a job this process no longer owns so polls fall through to storage."""
    with _enhanced_momentum_backtest_lock:
        _enhanced_momentum_backtest_jobs.pop(job_id, None)


def _enhanced_backtest_job_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "progress": _decode_enhanced_backtest_json(row.get("progress_json")),
        "run_id": int(row["run_id"]) if row.get("run_id") is not None else None,
        "error": row.get("error"),
        "submission_id": row.get("submission_id"),
        "request_fingerprint": row.get("request_fingerprint"),
        "worker_id": row.get("worker_id"),
        "heartbeat_at": row.get("heartbeat_at"),
    }


_ENHANCED_BACKTEST_JOB_SELECT = """
    SELECT job_id, submission_id, request_fingerprint, status, progress_json, run_id, error,
           worker_id, heartbeat_at, created_at, started_at, completed_at
    FROM enhanced_momentum_backtest_jobs
"""


def _load_enhanced_backtest_job(job_id: str) -> dict[str, Any] | None:
    row = get_db().fetch_one(_ENHANCED_BACKTEST_JOB_SELECT + " WHERE job_id = %(job_id)s", {"job_id": job_id})
    return _enhanced_backtest_job_from_row(row) if row else None


def _load_unfinished_enhanced_backtest_jobs() -> list[dict[str, Any]]:
    rows = get_db().fetch_all(
        _ENHANCED_BACKTEST_JOB_SELECT + " WHERE status IN ('queued', 'running') ORDER BY created_at",
    )
    return [_enhanced_backtest_job_from_row(row) for row in rows]


def _enhanced_backtest_worker_is_alive(job: dict[str, Any], now: datetime) -> bool:
    """True while another API process is still heartbeating this job.

    A job this process owns but no longer has in memory cannot be running
    here, so ownership alone is never taken as proof of life.
    """
    if job.get("worker_id") == _ENHANCED_BACKTEST_WORKER_ID:
        return False
    heartbeat = job.get("heartbeat_at") or job.get("created_at")
    if heartbeat is None:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return now - heartbeat < _ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT


def _settle_enhanced_backtest_job(job: dict[str, Any]) -> dict[str, Any]:
    """Resolve a persisted job whose worker does not live in this process.

    Terminal jobs are returned as saved. An unfinished job is left alone while
    its owner still heartbeats; once that stops it is settled from the only
    durable evidence: a run row linked to the job means it completed, no run
    means the worker died before saving and the job failed.

    Settlement is itself conditional in storage - the row must still be
    active and (for an interruption) its heartbeat still stale - so two
    processes settling at once, or a worker that heartbeats at the last
    moment, cannot produce conflicting outcomes. The row is re-read when the
    conditional write does not apply.
    """
    if job["status"] not in _ENHANCED_BACKTEST_ACTIVE_STATUSES:
        return job
    now = datetime.now(timezone.utc)
    if _enhanced_backtest_worker_is_alive(job, now):
        return job
    saved_run = get_db().fetch_one(
        "SELECT id FROM enhanced_momentum_backtest_runs WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
        (job["job_id"],),
    )
    if saved_run:
        run_id = int(saved_run["id"])
        changes: dict[str, Any] = {
            "status": "completed",
            "completed_at": now,
            "run_id": run_id,
            "error": None,
            "progress": {
                "percent": 100,
                "phase": "Complete",
                "completed": 1,
                "total": 1,
                "unit": "replay",
                "detail": f"Saved run #{run_id}",
            },
        }
        outcome = _write_enhanced_backtest_job(
            job["job_id"],
            changes,
            " WHERE job_id = %(job_id)s AND status IN ('queued', 'running')",
            {},
        )
        if outcome == "written":
            logger.info("Recovered enhanced momentum replay job {} as completed run #{}", job["job_id"], run_id)
    else:
        changes = {
            "status": "failed",
            "completed_at": now,
            "error": _ENHANCED_BACKTEST_INTERRUPTED_ERROR,
        }
        outcome = _write_enhanced_backtest_job(
            job["job_id"],
            changes,
            " WHERE job_id = %(job_id)s AND status IN ('queued', 'running')"
            " AND COALESCE(heartbeat_at, created_at) < %(heartbeat_before)s",
            {"heartbeat_before": now - _ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT},
        )
        if outcome == "written":
            logger.warning(
                "Enhanced momentum replay job {} was abandoned by worker {}; marked failed",
                job["job_id"],
                job.get("worker_id"),
            )
    if outcome == "written":
        return {**job, **changes}
    # Someone else settled it first, or the worker came back: report the row as it is now.
    return _load_enhanced_backtest_job(job["job_id"]) or {**job, **changes}


def _update_enhanced_backtest_job(job_id: str, **changes: Any) -> None:
    """Apply a change to a job this process owns and mirror it to storage.

    Progress-only updates are kept in memory (the owning process serves polls
    from there) and flushed by the heartbeat; anything else - status, outcome,
    timestamps - is written through immediately together with the latest
    progress and a fresh heartbeat. Every storage write is owner-fenced: if
    it is rejected the job is forfeited and ``_EnhancedBacktestOwnershipLost``
    is raised so the caller writes nothing more.
    """
    with _enhanced_momentum_backtest_lock:
        job = _enhanced_momentum_backtest_jobs.get(job_id)
        if job is None:
            raise _EnhancedBacktestOwnershipLost(job_id)
        next_progress = changes.pop("progress", None)
        if next_progress is not None:
            previous = job.get("progress") or {}
            next_progress = dict(next_progress)
            next_progress["percent"] = max(
                float(previous.get("percent") or 0),
                float(next_progress.get("percent") or 0),
            )
        if not changes:
            if next_progress is not None:
                job["progress"] = next_progress
            return
        persisted = {
            **changes,
            "progress": next_progress if next_progress is not None else dict(job.get("progress") or {}),
            "heartbeat_at": datetime.now(timezone.utc),
        }
    outcome = _persist_enhanced_backtest_job_changes(job_id, persisted)
    if outcome == "rejected":
        _forfeit_enhanced_backtest_job(job_id)
        raise _EnhancedBacktestOwnershipLost(job_id)
    with _enhanced_momentum_backtest_lock:
        job = _enhanced_momentum_backtest_jobs.get(job_id)
        if job is None:
            raise _EnhancedBacktestOwnershipLost(job_id)
        if next_progress is not None:
            job["progress"] = next_progress
        job.update(changes)


def _flush_enhanced_backtest_job_heartbeat(job_id: str) -> None:
    with _enhanced_momentum_backtest_lock:
        job = _enhanced_momentum_backtest_jobs.get(job_id)
        if job is None or job["status"] not in _ENHANCED_BACKTEST_ACTIVE_STATUSES:
            return
        snapshot = {"progress": dict(job.get("progress") or {}), "heartbeat_at": datetime.now(timezone.utc)}
    if _persist_enhanced_backtest_job_changes(job_id, snapshot) == "rejected":
        logger.warning("Enhanced momentum replay job {} was settled elsewhere; this worker forfeits it", job_id)
        _forfeit_enhanced_backtest_job(job_id)


def _enhanced_backtest_heartbeat_loop(job_id: str, stop: Event) -> None:
    while not stop.wait(_ENHANCED_BACKTEST_HEARTBEAT_INTERVAL.total_seconds()):
        _flush_enhanced_backtest_job_heartbeat(job_id)


def _run_enhanced_momentum_backtest_job(job_id: str, request: EnhancedMomentumBacktestRequest) -> None:
    try:
        _update_enhanced_backtest_job(
            job_id,
            status="running",
            started_at=datetime.now(timezone.utc),
            progress={
                "percent": 1,
                "phase": "Preparing replay",
                "completed": 0,
                "total": 0,
                "unit": "steps",
                "detail": "Preparing the historical session window",
            },
        )
    except _EnhancedBacktestOwnershipLost:
        logger.warning("Enhanced momentum replay job {} was settled before its worker started; not running it", job_id)
        return
    # The heartbeat uses ``threading.Thread`` directly: it must be a real
    # background thread even where the worker thread itself is substituted.
    stop_heartbeat = Event()
    threading.Thread(
        target=_enhanced_backtest_heartbeat_loop,
        args=(job_id, stop_heartbeat),
        name=f"enhanced-momentum-backtest-heartbeat-{job_id[:8]}",
        daemon=True,
    ).start()
    try:
        execute = (
            _execute_enhanced_momentum_buy_hold_backtest
            if request.mode == "buy_and_hold"
            else _execute_enhanced_momentum_backtest
        )
        response = execute(
            request,
            progress_callback=lambda progress: _update_enhanced_backtest_job(job_id, progress=progress),
            job_id=job_id,
        )
        _update_enhanced_backtest_job(
            job_id,
            status="completed",
            completed_at=datetime.now(timezone.utc),
            run_id=response["run"]["id"],
            progress={
                "percent": 100,
                "phase": "Complete",
                "completed": 1,
                "total": 1,
                "unit": "replay",
                "detail": f"Saved run #{response['run']['id']}",
            },
        )
    except _EnhancedBacktestOwnershipLost:
        # Settled or replaced by another API process while this worker was
        # stalled. Its outcome is already decided; say nothing more.
        logger.warning("Enhanced momentum replay job {} lost ownership mid-replay; discarding this worker's result", job_id)
        _forfeit_enhanced_backtest_job(job_id)
    except Exception as error:
        detail = error.detail if isinstance(error, HTTPException) else str(error)
        logger.exception("Enhanced momentum historical replay job failed")
        _finalise_enhanced_backtest_failure(job_id, str(detail))
    finally:
        stop_heartbeat.set()


def _finalise_enhanced_backtest_failure(job_id: str, detail: str) -> None:
    """Record a worker failure only after checking for a saved result.

    The result insert can commit and then lose its reply, and the retry can
    fail on the wire too; the replay still succeeded. So the saved-result
    evidence is consulted first and wins. If even that check cannot reach
    storage, no outcome is asserted: the job is forfeited from memory and
    left running in storage, where the first reader settles it from the
    same evidence once storage is back.
    """
    try:
        saved = get_db().fetch_one(
            "SELECT id FROM enhanced_momentum_backtest_runs WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        )
    except Exception as lookup_error:  # noqa: BLE001 - cannot verify; do not guess
        logger.warning(
            "Enhanced momentum replay job {} failed ({}) and its saved-result check is unavailable ({}); "
            "leaving it to be settled from storage",
            job_id, detail, lookup_error,
        )
        _forfeit_enhanced_backtest_job(job_id)
        return
    try:
        if saved:
            run_id = int(saved["id"])
            logger.warning(
                "Enhanced momentum replay job {} reported '{}' but its result is saved as run #{}; recording completion",
                job_id, detail, run_id,
            )
            _update_enhanced_backtest_job(
                job_id,
                status="completed",
                completed_at=datetime.now(timezone.utc),
                run_id=run_id,
                error=None,
                progress={
                    "percent": 100,
                    "phase": "Complete",
                    "completed": 1,
                    "total": 1,
                    "unit": "replay",
                    "detail": f"Saved run #{run_id}",
                },
            )
        else:
            _update_enhanced_backtest_job(
                job_id,
                status="failed",
                completed_at=datetime.now(timezone.utc),
                error=detail,
            )
    except _EnhancedBacktestOwnershipLost:
        logger.warning("Enhanced momentum replay job {} finished after losing ownership; outcome already settled", job_id)


def _active_enhanced_backtest_job_elsewhere() -> dict[str, Any] | None:
    """Find a replay still running in another API process, settling stale ones."""
    for job in _load_unfinished_enhanced_backtest_jobs():
        if job["job_id"] in _enhanced_momentum_backtest_jobs:
            continue
        settled = _settle_enhanced_backtest_job(job)
        if settled["status"] in _ENHANCED_BACKTEST_ACTIVE_STATUSES:
            return settled
    return None


def _launch_enhanced_backtest_worker(
    job_id: str,
    request: EnhancedMomentumBacktestRequest,
    response: dict[str, Any],
) -> dict[str, Any]:
    """Start the worker thread for an admitted job and return the handle.

    If the thread cannot be started the job must not linger as queued - in
    the local registry (blocking every later submission) or in storage. It
    is recorded as failed with the reason, durably and owner-fenced, then
    dropped from memory so this process answers for it from storage like
    any other. The caller receives that failed handle; a retry of the same
    submission reports the same outcome and starts nothing.
    """
    try:
        Thread(
            target=_run_enhanced_momentum_backtest_job,
            args=(job_id, request),
            name=f"enhanced-momentum-backtest-{job_id[:8]}",
            daemon=True,
        ).start()
    except Exception as error:  # noqa: BLE001 - report it, never leave the job queued
        detail = f"The replay worker could not be started: {error}. Submit again to retry."
        logger.exception("Enhanced momentum replay job {} could not start its worker", job_id)
        try:
            _update_enhanced_backtest_job(
                job_id,
                status="failed",
                completed_at=datetime.now(timezone.utc),
                error=detail,
            )
        except _EnhancedBacktestOwnershipLost:
            pass
        with _enhanced_momentum_backtest_lock:
            failed = _enhanced_momentum_backtest_jobs.pop(job_id, None)
        if failed is not None:
            return _enhanced_backtest_job_snapshot(failed)
        return _enhanced_backtest_job_snapshot({**response, "status": "failed", "error": detail})
    return response


def _resolve_enhanced_backtest_submission(
    existing: dict[str, Any],
    request: EnhancedMomentumBacktestRequest,
) -> dict[str, Any]:
    """Answer a retried submission with the job it already produced.

    A reused identity with different parameters is rejected (422, following
    the Idempotency-Key convention) so the caller cannot silently get a
    result for a different replay. Otherwise the original job is returned:
    from memory if this process runs it, from storage (settled if its worker
    is gone) otherwise. A job this process admitted but never started -
    admission committed, reply lost - is claimed and started now, once.
    """
    if existing.get("request_fingerprint") != _enhanced_backtest_request_fingerprint(request):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Submission {existing.get('submission_id')} was already used with different parameters. "
                "Use a new submission id for a different replay."
            ),
        )
    with _enhanced_momentum_backtest_lock:
        owned = _enhanced_momentum_backtest_jobs.get(existing["job_id"])
        if owned is not None:
            return _enhanced_backtest_job_snapshot(owned)
        orphaned_here = (
            existing["status"] == "queued"
            and existing.get("worker_id") == _ENHANCED_BACKTEST_WORKER_ID
        )
        if orphaned_here and _claim_enhanced_backtest_job(existing["job_id"]):
            job = {
                **{key: existing.get(key) for key in _ENHANCED_BACKTEST_JOB_RESPONSE_KEYS},
                "progress": dict(existing.get("progress") or {}),
            }
            _enhanced_momentum_backtest_jobs[job["job_id"]] = job
            response = _enhanced_backtest_job_snapshot(job)
            launch = True
        else:
            launch = False
    if launch:
        logger.info("Starting enhanced momentum replay job {} whose admission reply was lost", existing["job_id"])
        return _launch_enhanced_backtest_worker(existing["job_id"], request, response)
    return _enhanced_backtest_job_snapshot(_settle_enhanced_backtest_job(existing))


@app.post("/api/enhanced-momentum-scanner/backtest")
def start_enhanced_momentum_backtest(request: EnhancedMomentumBacktestRequest):
    """Start a historical replay and immediately return its progress handle.

    With a ``submission_id`` the call is idempotent: a retry returns the job
    the first call created, whatever its state, without starting a replay.
    """
    preset_ids = list(dict.fromkeys(request.preset_ids))
    available_presets = {preset.preset_id for preset in build_filter_presets()}
    unknown_presets = [preset_id for preset_id in preset_ids if preset_id not in available_presets]
    if unknown_presets:
        raise HTTPException(status_code=422, detail=f"Unknown Watchlist Analysis filter(s): {', '.join(unknown_presets)}")
    fundamental_setup_ids = list(dict.fromkeys(request.fundamental_setup_ids))
    invalid_fundamental_setups = [
        setup_id
        for setup_id in fundamental_setup_ids
        if setup_id not in SETUPS or setup_id in FILTER_BACKED_SETUP_IDS
    ]
    if invalid_fundamental_setups:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or filter-backed Fundamental Setup(s): {', '.join(invalid_fundamental_setups)}",
        )

    if request.submission_id is not None:
        existing = _load_enhanced_backtest_job_by_submission(request.submission_id)
        if existing is not None:
            return _resolve_enhanced_backtest_submission(existing, request)

    with _enhanced_momentum_backtest_lock:
        busy = (
            any(job["status"] in _ENHANCED_BACKTEST_ACTIVE_STATUSES for job in _enhanced_momentum_backtest_jobs.values())
            or _active_enhanced_backtest_job_elsewhere() is not None
        )
        if busy:
            admission = "busy"
        else:
            job_id, job, admission, response = _admit_new_enhanced_backtest_job(request)

    if admission != "admitted":
        # Storage is the arbiter. The identity lookup above may have run
        # before a concurrent submission with the same identity committed -
        # in this process or another - so re-check it before refusing: a
        # retry gets the original job, a genuinely different submission 409.
        return _answer_enhanced_backtest_submission_when_busy(request)

    return _launch_enhanced_backtest_worker(job_id, request, response)


def _answer_enhanced_backtest_submission_when_busy(request: EnhancedMomentumBacktestRequest) -> dict[str, Any]:
    if request.submission_id is not None:
        existing = _load_enhanced_backtest_job_by_submission(request.submission_id)
        if existing is not None:
            return _resolve_enhanced_backtest_submission(existing, request)
    raise HTTPException(status_code=409, detail=_ENHANCED_BACKTEST_BUSY_DETAIL)


def _admit_new_enhanced_backtest_job(
    request: EnhancedMomentumBacktestRequest,
) -> tuple[str, dict[str, Any], str, dict[str, Any] | None]:
    """Create, persist and register a job.

    The caller holds the (non-reentrant) registry lock so in-process
    submissions are serialised; storage still arbitrates across processes.
    """
    while len(_enhanced_momentum_backtest_jobs) >= _MAX_ENHANCED_MOMENTUM_BACKTEST_JOBS:
        terminal_ids = [
            known_id
            for known_id, job in _enhanced_momentum_backtest_jobs.items()
            if job["status"] not in _ENHANCED_BACKTEST_ACTIVE_STATUSES
        ]
        if not terminal_ids:
            break
        oldest_id = min(terminal_ids, key=lambda known_id: _enhanced_momentum_backtest_jobs[known_id]["created_at"])
        del _enhanced_momentum_backtest_jobs[oldest_id]

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "submission_id": request.submission_id,
        "request_fingerprint": _enhanced_backtest_request_fingerprint(request),
        "status": "queued",
        "created_at": datetime.now(timezone.utc),
        "started_at": None,
        "completed_at": None,
        "progress": {
            "percent": 0,
            "phase": "Queued",
            "completed": 0,
            "total": 0,
            "unit": "steps",
            "detail": "Waiting for the replay worker",
        },
        "run_id": None,
        "error": None,
    }
    try:
        admission = _admit_enhanced_backtest_job(job, request)
    except Exception as error:  # noqa: BLE001 - surface as a service error, never hand out an unsaved handle
        logger.exception("Could not record enhanced momentum replay job before starting it")
        raise HTTPException(status_code=503, detail="The replay job could not be recorded. Try again shortly.") from error
    response = None
    if admission == "admitted":
        _enhanced_momentum_backtest_jobs[job_id] = job
        response = _enhanced_backtest_job_snapshot(job)
    return job_id, job, admission, response


@app.get("/api/enhanced-momentum-scanner/backtest-jobs/{job_id}")
def get_enhanced_momentum_backtest_job(job_id: str):
    """Return the current phase and progress of a historical replay.

    Jobs owned by this process are answered from memory. Any other job is
    read from storage, so a handle survives an API restart: a completed job
    resolves to its saved run without re-running anything, a job still
    heartbeating in another process is reported as running, and an abandoned
    one is settled as completed or failed from the durable evidence.
    """
    with _enhanced_momentum_backtest_lock:
        job = _enhanced_momentum_backtest_jobs.get(job_id)
        if job is not None:
            return _enhanced_backtest_job_snapshot(job)
    job = _load_enhanced_backtest_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Enhanced momentum replay was not found or has expired")
    return _enhanced_backtest_job_snapshot(_settle_enhanced_backtest_job(job))


@app.get("/api/enhanced-momentum-scanner/backtests")
def get_enhanced_momentum_backtests(limit: int = Query(default=50, ge=1, le=200)):
    rows = get_db().fetch_all(
        """
        SELECT id, signal_cutoff_date, end_date, lookback_months,
               filters_json, summary_json, created_at
        FROM enhanced_momentum_backtest_runs
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    runs = []
    for row in rows:
        summary = _decode_enhanced_backtest_json(row.get("summary_json"))
        filters = _decode_enhanced_backtest_json(row.get("filters_json"))
        runs.append({
            "id": int(row["id"]),
            "signal_cutoff_date": str(row["signal_cutoff_date"])[:10],
            "end_date": str(row["end_date"])[:10],
            "lookback_months": int(row["lookback_months"]),
            "filters": filters,
            "summary": summary,
            "created_at": _serialize_row({"created_at": row.get("created_at")})["created_at"],
        })
    return {"runs": runs}


@app.get("/api/enhanced-momentum-scanner/backtests/{run_id}")
def get_enhanced_momentum_backtest(run_id: int):
    row = get_db().fetch_one(
        """
        SELECT id, signal_cutoff_date, end_date, lookback_months,
               filters_json, config_json, summary_json, scan_json,
               results_json, strategies_json, job_id, created_at
        FROM enhanced_momentum_backtest_runs WHERE id = %s
        """,
        (run_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Enhanced momentum backtest {run_id} not found")
    return {"run": {
        "id": int(row["id"]),
        "job_id": row.get("job_id"),
        "signal_cutoff_date": str(row["signal_cutoff_date"])[:10],
        "end_date": str(row["end_date"])[:10],
        "lookback_months": int(row["lookback_months"]),
        "filters": _decode_enhanced_backtest_json(row.get("filters_json")),
        "config": _decode_enhanced_backtest_json(row.get("config_json")),
        "summary": _decode_enhanced_backtest_json(row.get("summary_json")),
        "scan": _decode_enhanced_backtest_json(row.get("scan_json")),
        "results": _decode_enhanced_backtest_json(row.get("results_json")),
        "strategies": _decode_enhanced_backtest_json(row.get("strategies_json")),
        "preset_ids": _decode_enhanced_backtest_json(row.get("filters_json")).get("preset_ids", []),
        "fundamental_setup_ids": _decode_enhanced_backtest_json(row.get("filters_json")).get("fundamental_setup_ids", []),
        "created_at": _serialize_row({"created_at": row.get("created_at")})["created_at"],
    }}


@app.delete("/api/enhanced-momentum-scanner/backtests/{run_id}")
def delete_enhanced_momentum_backtest(run_id: int):
    affected = get_db().execute("DELETE FROM enhanced_momentum_backtest_runs WHERE id = %s", (run_id,))
    if affected == 0:
        raise HTTPException(status_code=404, detail=f"Enhanced momentum backtest {run_id} not found")
    return {"deleted": True, "run_id": run_id}


@app.get("/api/stocks/{symbol}")
def get_stock(
    symbol: str,
    stock_id: int | None = Query(None, ge=1),
):
    """Get detailed stock info with latest indicators."""
    db = get_db()
    resolved = _resolve_stock_id(db, symbol, stock_id=stock_id)
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

    result = _serialize_stock_row(stock)
    result["sector"] = _stock_sector_map(db, [stock["id"]]).get(int(stock["id"]))
    result["indicators"] = _serialize_row(indicators) if indicators else None
    result["latest_candle"] = _serialize_row(latest_candle) if latest_candle else None
    result["prev_close"] = float(prev_candle["close"]) if prev_candle else None
    return result


@app.get("/api/stocks/{symbol}/fundamentals")
def get_stock_fundamentals(
    symbol: str,
    line_limit: int = Query(2000, ge=1, le=10000),
    stock_id: int | None = Query(None, ge=1),
):
    """Return the stored Upstox profile, statements, ratios and histories."""
    db = get_db()
    stock = _resolve_stock_id(db, symbol, stock_id=stock_id)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    fundamentals = store("fundamental").get_stock_fundamentals(
        stock_id=stock["id"],
        line_limit=line_limit,
    )
    competitors = fundamentals.get("competitors") or []
    competitor_isins = list(dict.fromkeys(
        str(item.get("competitor_isin") or "").strip().upper()
        for item in competitors
        if item.get("competitor_isin")
    ))
    peers_by_isin: dict[str, dict[str, Any]] = {}
    if competitor_isins:
        peer_rows = db.fetch_all(
            """
            WITH peer_stocks AS (
                SELECT DISTINCT ON (UPPER(s.isin))
                       s.id, UPPER(s.isin) AS isin, s.symbol, s.name,
                       s.market_cap_cr
                FROM stocks s
                WHERE UPPER(s.isin) = ANY(%s)
                  AND s.exchange = 'NSE'
                ORDER BY UPPER(s.isin),
                         CASE WHEN UPPER(COALESCE(s.series, '')) = 'EQ' THEN 0 ELSE 1 END,
                         s.is_active DESC, s.id
            )
            SELECT peer.isin, peer.symbol, peer.name,
                   peer.market_cap_cr,
                   pe.company_value AS pe_ratio,
                   pe.company_raw AS pe_raw,
                   pe.snapshot_date AS pe_snapshot_date
            FROM peer_stocks peer
            LEFT JOIN LATERAL (
                SELECT r.company_value, r.company_raw, r.snapshot_date
                FROM company_fundamental_ratios r
                WHERE r.stock_id = peer.id
                  AND r.source = 'upstox'
                  AND REGEXP_REPLACE(LOWER(r.ratio_name), '[^a-z0-9]', '', 'g')
                      IN ('pe', 'peratio', 'pricetoearnings', 'priceearningsratio')
                ORDER BY r.snapshot_date DESC, r.fetched_at DESC
                LIMIT 1
            ) pe ON TRUE
            """,
            (competitor_isins,),
        )
        peers_by_isin = {str(row.get("isin") or "").upper(): row for row in peer_rows}

    for competitor in competitors:
        peer = peers_by_isin.get(str(competitor.get("competitor_isin") or "").upper())
        company_name = None
        if peer:
            company_name = _stock_company_name(peer.get("symbol"), peer.get("name"))
            competitor["symbol"] = peer.get("symbol")
            competitor["market_cap_cr"] = peer.get("market_cap_cr")
            competitor["pe_ratio"] = peer.get("pe_ratio")
            competitor["pe_snapshot_date"] = peer.get("pe_snapshot_date")
        if not company_name:
            profile = str(competitor.get("company_profile") or "").strip()
            first_clause = re.match(r"^(.{1,140}?)\s+is\b", profile, re.IGNORECASE)
            if first_clause:
                company_name = re.sub(r"\s*\([^)]*\)", "", first_clause.group(1)).strip(" ,;:-")
        competitor["company_name"] = company_name or ""

    return {
        "symbol": stock["symbol"],
        "name": stock.get("name", ""),
        "company_name": _stock_company_name(stock["symbol"], stock.get("name")) or "",
        "sector": _stock_sector_map(db, [stock["id"]]).get(int(stock["id"])),
        "isin": stock.get("isin"),
        "source": "upstox",
        "fundamentals": _serialize_row(fundamentals),
    }


@app.post("/api/stocks/fundamentals/score-inputs")
def get_fundamental_score_inputs(request: FundamentalScoreInputsRequest):
    """Return the compact, batched Upstox inputs used by scanner row scores."""
    unique_ids = list(dict.fromkeys(request.stock_ids))
    inputs = store("fundamental").get_fundamental_score_inputs(unique_ids)
    return {"score_inputs": _serialize_row(inputs)}


@app.get("/api/system/fundamentals-sync")
def get_fundamentals_sync_status(
    limit: int = Query(20, ge=1, le=100),
):
    """Return recent fundamentals ingestion runs for monitoring."""
    from data.fundamentals.service import FundamentalsSyncService

    fundamentals = store("fundamental")
    return {
        "source": "upstox",
        "configured": _fundamentals_service is not None,
        "scheduler_active": _fundamentals_scheduler is not None,
        "coverage": [
            _serialize_row(row)
            for row in fundamentals.get_sync_coverage(FundamentalsSyncService.ENDPOINTS)
        ],
        "runs": [_serialize_row(row) for row in fundamentals.get_sync_status(limit)],
    }


# ── Candles (Chart Data) ──────────────────────────────────────

@app.get("/api/stocks/{symbol}/candles")
def get_candles(
    symbol: str,
    interval: str = Query("1d", description="Candle interval: 1m, 5m, 15m, 1h, 1d, 1w"),
    days: int = Query(90, ge=1, le=730, description="Days of history"),
    stock_id: int | None = Query(None, ge=1),
):
    """Get OHLCV candles for charting."""
    db = get_db()

    if interval not in TABLE_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval '{interval}'. Use: {', '.join(TABLE_MAP.keys())}",
        )

    stock = _resolve_stock_id(db, symbol, stock_id=stock_id)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")

    table = TABLE_MAP[interval]
    session_filter_sql = ""
    if interval in INTRADAY_TIMEFRAMES:
        session_filter_sql = (
            "AND (timestamp AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15' "
            "AND (timestamp AT TIME ZONE 'Asia/Kolkata')::time <= TIME '15:30'"
        )
    rows = db.fetch_all(
        f"""
        SELECT timestamp, open, high, low, close, volume, vwap
        FROM {table}
          WHERE stock_id = %s
          AND timestamp >= NOW() - INTERVAL '{days} days'
          {session_filter_sql}
        ORDER BY timestamp ASC
        """,
        (stock["id"],),
    )

    if interval == Timeframe.ONE_DAY.value:
        rows, _ = normalize_daily_candles(rows)
    elif interval in INTRADAY_TIMEFRAMES:
        rows = [row for row in rows if is_regular_session_candle(row["timestamp"])]

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
    days: int = Query(1095, ge=1, le=1095, description="Days of history to fetch"),
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
            stock={
                "id": stock["id"],
                "symbol": stock["symbol"],
                "token": stock["token"],
                "exchange": stock["exchange"],
            },
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
        f"""
        SELECT wl.id, wl.name, wl.description, wl.list_type, wl.is_active, wl.max_stocks,
               COUNT(wi.id) FILTER (WHERE {equity_universe_sql('s')}) AS stock_count
        FROM watchlists wl
        LEFT JOIN watchlist_items wi ON wi.watchlist_id = wl.id
        LEFT JOIN stocks s ON s.id = wi.stock_id
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
        f"""
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
        WHERE wi.watchlist_id = %s AND {equity_universe_sql('s')}
        ORDER BY wi.priority DESC, s.symbol
        """,
        (watchlist_id,),
    )
    _attach_stock_sectors(db, rows, stock_id_key="id")
    return {"stocks": [_serialize_stock_row(r) for r in rows]}


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
        f"""
        WITH wl_stocks AS (
            SELECT s.id AS stock_id, s.token
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wi.watchlist_id = %s AND {equity_universe_sql('s')}
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
        f"""
        WITH wl_stocks AS (
            SELECT wi.stock_id, s.symbol, s.name
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wi.watchlist_id = %s AND {equity_universe_sql('s')}
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
    sector_by_stock_id = _stock_sector_map(db, stock_ids)

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
            "company_name": _stock_company_name(r["symbol"], r["name"]) or "",
            "sector": sector_by_stock_id.get(int(r["stock_id"])),
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


class RenameWatchlistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@app.put("/api/watchlists/{watchlist_id}")
def rename_watchlist(watchlist_id: int, req: RenameWatchlistRequest):
    """Rename an active watchlist."""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Watchlist name is required")

    db = get_db()
    current = db.fetch_one(
        "SELECT id FROM watchlists WHERE id = %s AND is_active = TRUE",
        (watchlist_id,),
    )
    if not current:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    duplicate = db.fetch_one(
        "SELECT id FROM watchlists WHERE name = %s AND id <> %s",
        (name, watchlist_id),
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="A watchlist with this name already exists")

    updated = db.execute_returning(
        """
        UPDATE watchlists
        SET name = %s, updated_at = NOW()
        WHERE id = %s AND is_active = TRUE
        RETURNING id, name
        """,
        (name, watchlist_id),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return {"id": updated["id"], "name": updated["name"], "updated": True}


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


class BulkAddStocksRequest(BaseModel):
    stock_ids: list[int] = Field(min_length=1, max_length=500)
    source: str = "momentum_scanner"


@app.post("/api/watchlists/{watchlist_id}/stocks")
def add_stock_to_watchlist(watchlist_id: int, req: AddStockRequest):
    """Add a stock to a watchlist. Auto-backfills candle data if missing."""
    db = get_db()
    wl = db.fetch_one("SELECT name, max_stocks FROM watchlists WHERE id = %s AND is_active = TRUE", (watchlist_id,))
    if not wl:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")

    # Verify stock exists
    stock = db.fetch_one(
        f"SELECT id, symbol, token, exchange FROM stocks WHERE id = %s AND {equity_universe_sql('stocks')}",
        (req.stock_id,),
    )
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


@app.post("/api/watchlists/{watchlist_id}/stocks/bulk")
def bulk_add_stocks_to_watchlist(watchlist_id: int, req: BulkAddStocksRequest):
    """Add selected scanner results to a watchlist in one user action."""
    db = get_db()
    wl = db.fetch_one(
        "SELECT id, name, max_stocks FROM watchlists WHERE id = %s AND is_active = TRUE",
        (watchlist_id,),
    )
    if not wl:
        raise HTTPException(status_code=404, detail=f"Watchlist {watchlist_id} not found")

    requested_ids = list(dict.fromkeys(req.stock_ids))
    stock_rows = db.fetch_all(
        f"SELECT id, symbol FROM stocks WHERE id = ANY(%s) AND {equity_universe_sql('stocks')}",
        (requested_ids,),
    )
    stocks_by_id = {row["id"]: row for row in stock_rows}
    unknown_ids = [stock_id for stock_id in requested_ids if stock_id not in stocks_by_id]
    if unknown_ids:
        raise HTTPException(status_code=404, detail=f"Stock id(s) not found: {', '.join(map(str, unknown_ids))}")

    existing_rows = db.fetch_all(
        "SELECT stock_id FROM watchlist_items WHERE watchlist_id = %s AND stock_id = ANY(%s)",
        (watchlist_id, requested_ids),
    )
    existing_ids = {row["stock_id"] for row in existing_rows}
    current_count = db.fetch_one(
        "SELECT COUNT(*) AS count FROM watchlist_items WHERE watchlist_id = %s",
        (watchlist_id,),
    )["count"]
    capacity = max(0, int(wl["max_stocks"]) - int(current_count)) if wl["max_stocks"] > 0 else len(requested_ids)
    candidates = [stock_id for stock_id in requested_ids if stock_id not in existing_ids]
    to_add = candidates[:capacity]
    ws: WatchlistStore = store("watchlist")
    added_ids = [stock_id for stock_id in to_add if ws.add_stock(wl["name"], stock_id, req.source)]
    return {
        "watchlist_id": watchlist_id,
        "watchlist_name": wl["name"],
        "added_count": len(added_ids),
        "added_symbols": [stocks_by_id[stock_id]["symbol"] for stock_id in added_ids],
        "already_present_count": len(existing_ids),
        "capacity_skipped_count": len(candidates) - len(to_add),
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
            f"""
            SELECT id FROM stocks
            WHERE {equity_universe_sql('stocks')}
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
    conditions = [equity_universe_sql("s")]
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
    _attach_stock_sectors(db, rows)
    return {"signals": [_serialize_row(r) for r in rows]}


@app.get("/api/scanner-signals")
def list_scanner_signals(
    date_filter: Optional[str] = Query(None, alias="date", description="Filter by target_date"),
    limit: int = Query(50, ge=1, le=200),
):
    """Get scanner signals (overnight batch results)."""
    db = get_db()
    params = []
    conditions = [equity_universe_sql("s")]

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
    _attach_stock_sectors(db, rows)
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
    _attach_stock_sectors(db, rows)
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
    _attach_stock_sectors(db, rows)
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

_LIVE_CANDLE_INTERVALS = (
    CandleInterval.ONE_MINUTE,
    CandleInterval.FIVE_MINUTE,
    CandleInterval.FIFTEEN_MINUTE,
    CandleInterval.ONE_HOUR,
    CandleInterval.ONE_DAY,
    CandleInterval.ONE_WEEK,
)


def _get_live_candle_coverage(db: Database) -> list[dict]:
    """Read lightweight per-stock coverage metadata during ingestion.

    Counting every candle in every source table made the status endpoint
    compete with the background backfill for database connections and caused
    the frontend health check to time out. Coverage is updated after each
    successful stock fetch, so it is both current enough for health reporting
    and far cheaper to query.
    """
    coverage_store = _stores.get("coverage")
    if coverage_store is not None:
        return coverage_store.get_summary()

    # Keep status usable for lightweight test doubles and older API workers
    # that have not registered the coverage store yet. This fallback is more
    # expensive, so the normal application path above remains preferred.
    queries = []
    for interval in _LIVE_CANDLE_INTERVALS:
        suffix = CANDLE_TABLE_SUFFIX[interval]
        queries.append(
            f"""
            SELECT '{interval.value}' AS interval,
                   COUNT(DISTINCT stock_id) AS stock_count,
                   MIN(timestamp) AS global_earliest,
                   MAX(timestamp) AS global_latest,
                   COUNT(*) AS total_candles
            FROM candles_{suffix}
            """
        )
    return db.fetch_all(" UNION ALL ".join(queries) + " ORDER BY interval")


@app.get("/api/system/status")
def get_system_status():
    """Get system health and status."""
    db = get_db()
    healthy = db.health_check()

    # Stock count
    stock_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM stocks WHERE is_active = TRUE")

    live_coverage = _get_live_candle_coverage(db)
    coverage = [
        {
            "interval": row["interval"],
            "stocks": row["stock_count"],
            "candles": row["total_candles"],
        }
        for row in live_coverage
    ]

    return {
        "status": "running" if healthy else "degraded",
        "database": "connected" if healthy else "disconnected",
        "stocks_active": stock_count["cnt"] if stock_count else 0,
        "coverage": [_serialize_row(c) for c in coverage],
        "candle_sync": _candle_sync_snapshot(),
    }


@app.get("/api/system/candle-sync")
def get_candle_sync_status():
    """Get the current or most recent candle synchronization status."""
    return _candle_sync_snapshot()


@app.get("/api/system/config")
def get_system_config():
    """Get all app config values."""
    db = get_db()
    rows = db.fetch_all(
        "SELECT key, value, data_type, category, description FROM app_config ORDER BY category, key"
    )
    return {"config": [_serialize_row(r) for r in rows]}


@app.get("/api/trading/charges")
def get_trading_charges():
    """Return the active, persisted delivery transaction-cost schedule."""
    config = config_from_store(store("config"))
    return {
        "config": config.to_dict(),
        "formula": {
            "brokerage": "order_turnover × brokerage_rate (delivery brokerage is currently 0)",
            "gst": "gst_rate × (brokerage + transaction + IPFT + SEBI + DP service charge)",
            "buy_total": "brokerage + STT + transaction + IPFT + SEBI + stamp duty + GST",
            "sell_total": "brokerage + STT + transaction + IPFT + SEBI + DP charge + GST",
            "rounding": "STT and stamp duty round to the nearest rupee; other charges round to the nearest paise",
        },
    }


@app.put("/api/trading/charges")
def update_trading_charges(request: TradingChargesUpdateRequest):
    """Persist the edited master schedule used by all backtest entry points."""
    updates = request.model_dump(exclude_none=True)
    if not updates:
        return get_trading_charges()

    current = config_from_store(store("config")).to_dict()
    current.update(updates)
    try:
        validated = DeliveryChargeConfig(**current)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    config_store = store("config")
    for field_name, value in validated.to_dict().items():
        if field_name not in updates:
            continue
        if isinstance(value, bool):
            serialized = "true" if value else "false"
        else:
            serialized = str(value)
        config_store.set(
            f"charges_{field_name}",
            serialized,
            category="trading_costs",
        )
    return get_trading_charges()


@app.get("/api/coverage")
def get_coverage_summary():
    """Get candle data coverage summary."""
    db = get_db()
    rows = _get_live_candle_coverage(db)
    return {"coverage": [_serialize_row(r) for r in rows]}


# ══════════════════════════════════════════════════════════════
# FILTER CATALOG, PROFILES, AND PREVIEW
# ══════════════════════════════════════════════════════════════


@app.get("/api/filters/catalog")
def list_filter_catalog():
    """Return every known filter, including clearly marked roadmap entries."""
    filters = [item.model_dump(mode="json") for item in _FILTER_CATALOG.list()]
    return {
        "filters": filters,
        "available_count": sum(item["availability"] == "available" for item in filters),
        "planned_count": sum(item["availability"] == "planned" for item in filters),
        "categories": sorted({item["category"] for item in filters}),
    }


@app.get("/api/filter-backtest-presets")
def list_filter_backtest_presets():
    """Return curated short-term combinations for the dedicated backtest tab."""
    return {"presets": [preset.model_dump(mode="json") for preset in build_filter_presets()]}


@app.post("/api/filters/requirements")
def get_filter_requirements(req: FilterRequirementsRequest):
    """Validate an expression and return its maximum data requirements."""
    _, requirements, errors = _parse_filter_expression(req)
    return {"valid": not errors, "requirements": requirements, "errors": errors}


@app.post("/api/filter-profiles/validate")
def validate_filter_profile(req: FilterRequirementsRequest):
    """Validate a profile expression without persisting it."""
    expression, requirements, errors = _parse_filter_expression(req)
    return {
        "valid": not errors,
        "expression": expression.model_dump(mode="json") if expression else None,
        "requirements": requirements,
        "errors": errors,
    }


@app.get("/api/filter-profiles")
def list_filter_profiles(active_only: bool = True):
    rows = store("filter_profile").list_profiles(active_only=active_only)
    return {"profiles": [_json_profile(row) for row in rows]}


@app.post("/api/filter-profiles")
def create_filter_profile(req: FilterProfileCreateRequest):
    validation = FilterRequirementsRequest(mode=req.mode, expression=req.expression)
    expression, requirements, errors = _parse_filter_expression(validation)
    if errors or expression is None:
        raise HTTPException(status_code=422, detail={"errors": errors})
    profile = store("filter_profile").create_profile(
        name=req.name,
        description=req.description,
        mode=req.mode.value,
        owner_id=req.owner_id,
        expression=expression.model_dump(mode="json"),
        requirements=requirements,
        risk_config=req.risk_config,
        timeframe_config=req.timeframe_config,
    )
    return {"profile": _json_profile(profile), "requirements": requirements}


@app.get("/api/filter-profiles/{profile_id}")
def get_filter_profile(profile_id: int):
    profile = store("filter_profile").get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Filter profile {profile_id} not found")
    return {"profile": _json_profile(profile)}


@app.put("/api/filter-profiles/{profile_id}/draft")
def update_filter_profile_draft(profile_id: int, req: FilterProfileDraftRequest):
    validation = FilterRequirementsRequest(mode=req.mode, expression=req.expression)
    expression, requirements, errors = _parse_filter_expression(validation)
    if errors or expression is None:
        raise HTTPException(status_code=422, detail={"errors": errors})
    try:
        version = store("filter_profile").update_draft(
            profile_id=profile_id,
            expression=expression.model_dump(mode="json"),
            requirements=requirements,
            risk_config=req.risk_config,
            timeframe_config=req.timeframe_config,
            expected_version_id=req.expected_version_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version": _json_profile(version), "requirements": requirements}


@app.post("/api/filter-profiles/{profile_id}/publish")
def publish_filter_profile(profile_id: int, req: FilterProfilePublishRequest):
    profile_store = store("filter_profile")
    version = profile_store.get_version(profile_id, req.version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Filter profile version not found")
    try:
        expression = FilterExpression.model_validate(version.get("expression_json", {}))
        _FILTER_CATALOG.validate_expression(expression, req.mode)
        published = profile_store.publish(profile_id=profile_id, version_id=req.version_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"version": _json_profile(published)}


@app.post("/api/filter-profiles/{profile_id}/archive")
def archive_filter_profile(profile_id: int):
    archived = store("filter_profile").archive_profile(profile_id)
    if not archived:
        raise HTTPException(status_code=404, detail=f"Filter profile {profile_id} not found")
    return {"profile_id": profile_id, "archived": True}


@app.get("/api/filter-profiles/{profile_id}/bindings")
def get_filter_profile_bindings(profile_id: int):
    rows = store("filter_profile").get_bindings(profile_id)
    return {"bindings": [_json_profile(row) for row in rows]}


@app.put("/api/filter-profiles/{profile_id}/bindings")
def put_filter_profile_binding(profile_id: int, req: FilterProfileBindingRequest):
    try:
        binding = store("filter_profile").upsert_binding(
            profile_id=profile_id,
            version_id=req.version_id,
            target_type=req.target_type,
            target_id=req.target_id,
            direction=req.direction,
            enabled=req.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"binding": _json_profile(binding)}


@app.post("/api/filter-profiles/{profile_id}/preview")
def preview_filter_profile(profile_id: int, req: FilterPreviewRequest):
    """Evaluate one saved profile against requested symbols using stored candles."""
    profile_store = store("filter_profile")
    version = profile_store.get_version(profile_id)
    if not version:
        raise HTTPException(status_code=404, detail="Filter profile version not found")
    try:
        expression = FilterExpression.model_validate(version.get("expression_json", {}))
        mode_request = FilterRequirementsRequest(mode=req.mode, expression=expression.model_dump(mode="json"))
        _, requirements, errors = _parse_filter_expression(mode_request)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    db = get_db()
    as_of = req.as_of or datetime.now().astimezone()
    symbols = req.symbols
    if not symbols:
        rows = db.fetch_all(
            f"SELECT symbol FROM stocks WHERE {equity_universe_sql('stocks')} ORDER BY symbol LIMIT %s",
            (min(req.limit, 500),),
        )
        symbols = [row["symbol"] for row in rows]
    symbols = symbols[: min(req.limit, 500)]
    evaluator = FilterEvaluator(_FILTER_CATALOG)
    results = []
    result_stock_ids: dict[str, int] = {}
    for symbol in symbols:
        stock = _resolve_stock_id(db, symbol)
        if not stock or not is_equity_stock(stock):
            results.append({"symbol": symbol, "status": "unavailable", "reason": "stock not found"})
            continue
        candles_by_timeframe = {}
        for timeframe_name in requirements:
            table = _PREVIEW_TABLES[timeframe_name]
            rows = db.fetch_all(
                f"""
                SELECT timestamp, open, high, low, close, volume
                FROM {table}
                WHERE stock_id = %s AND timestamp <= %s
                ORDER BY timestamp ASC
                """,
                (stock["id"], as_of),
            )
            candles_by_timeframe[Timeframe(timeframe_name)] = rows
        context = build_evaluation_context(
            symbol=stock["symbol"],
            as_of=as_of,
            mode=req.mode,
            candles=candles_by_timeframe,
            stock_metadata=stock,
        )
        result = evaluator.evaluate(expression, context)
        result_payload = result.model_dump(mode="json")
        result_payload["company_name"] = _stock_company_name(stock["symbol"], stock.get("name")) or ""
        result_stock_ids[stock["symbol"]] = int(stock["id"])
        results.append(result_payload)
    sector_by_stock_id = _stock_sector_map(db, list(result_stock_ids.values()))
    for result_payload in results:
        stock_id = result_stock_ids.get(result_payload.get("symbol"))
        result_payload["sector"] = sector_by_stock_id.get(stock_id) if stock_id is not None else None
    results.sort(key=lambda item: item.get("score", 0), reverse=True)
    counts = {
        "pass": sum(item.get("status") == "pass" for item in results),
        "fail": sum(item.get("status") == "fail" for item in results),
        "insufficient_data": sum(item.get("status") == "insufficient_data" for item in results),
        "unavailable": sum(item.get("status") == "unavailable" for item in results),
    }
    return {
        "profile_id": profile_id,
        "version_id": version.get("id"),
        "as_of": as_of.isoformat(),
        "requirements": requirements,
        "counts": counts,
        "results": results,
    }


def _resolved_exit_config(
    request: FilterBacktestRequest,
    defaults: dict[str, float | int] | None = None,
) -> dict[str, float | int]:
    """Resolve safe exit defaults while allowing preset-specific policies."""
    fallback = defaults or {
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
        "max_hold_bars": 10,
    }
    return {
        "stop_loss_pct": request.stop_loss_pct if request.stop_loss_pct is not None else fallback["stop_loss_pct"],
        "take_profit_pct": request.take_profit_pct if request.take_profit_pct is not None else fallback["take_profit_pct"],
        "max_hold_bars": request.max_hold_bars if request.max_hold_bars is not None else fallback["max_hold_bars"],
    }


def _backtest_date_bounds(req: FilterBacktestRequest) -> tuple[datetime, datetime]:
    """Parse the API date contract once for a backtest request."""
    def boundary(value: str, *, end: bool = False) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            # Date-only backtest inputs describe NSE session dates, not UTC
            # dates. Interpret them in exchange time so daily close bars at
            # 15:30 IST remain inside the requested day.
            parsed = parsed.replace(tzinfo=IST)
        if end and len(value) == 10:
            parsed += timedelta(days=1) - timedelta(microseconds=1)
            now = datetime.now(IST)
            if parsed.date() >= now.date() and now.time().replace(tzinfo=None) < MARKET_CLOSE:
                parsed = datetime.combine(now.date() - timedelta(days=1), time.max, tzinfo=IST)
        return parsed

    try:
        start_dt = boundary(req.start_date)
        end_dt = boundary(req.end_date, end=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD or ISO timestamps") from exc
    if end_dt < start_dt:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    return start_dt, end_dt


def _load_filter_backtest_inputs(
    req: FilterBacktestRequest,
    requirements: dict[str, dict[str, Any]],
    execution_timeframe: Timeframe | None = None,
    quality_counts: dict[str, int] | None = None,
    warmup_candles_by_timeframe: dict[str, int] | None = None,
):
    """Load and normalize candle inputs once for one or many presets."""
    start_dt, end_dt = _backtest_date_bounds(req)
    db = get_db()
    stock = _resolve_stock_id(db, req.symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {req.symbol} not found")

    required_timeframes = set(requirements)
    if execution_timeframe is not None:
        required_timeframes.add(execution_timeframe.value)
    candles_by_timeframe: dict[Timeframe, list[CandlePoint]] = {}
    for timeframe_name in required_timeframes:
        table = _PREVIEW_TABLES[timeframe_name]
        is_daily = timeframe_name == Timeframe.ONE_DAY.value
        session_filter = ""
        if timeframe_name in INTRADAY_TIMEFRAMES:
            session_filter = (
                " AND (timestamp AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15'"
                " AND (timestamp AT TIME ZONE 'Asia/Kolkata')::time <= TIME '15:30'"
            )

        if is_daily:
            # Load complete daily history through the requested end date so
            # warm-up is counted in sessions after duplicate dates are folded.
            # The stored timestamp may be midnight or another provider label;
            # it is not the point at which a full-day OHLC candle is available.
            rows = db.fetch_all(
                f"""
                SELECT timestamp, open, high, low, close, volume
                FROM {table}
                WHERE stock_id = %s
                  AND (timestamp AT TIME ZONE 'Asia/Kolkata')::date <= %s
                ORDER BY timestamp ASC
                """,
                (stock["id"], end_dt.astimezone(IST).date()),
            )
        elif warmup_candles_by_timeframe is None:
            rows = db.fetch_all(
                f"""
                SELECT timestamp, open, high, low, close, volume
                FROM {table}
                WHERE stock_id = %s AND timestamp <= %s{session_filter}
                ORDER BY timestamp ASC
                """,
                (stock["id"], end_dt),
            )
        else:
            warmup_count = max(0, int(warmup_candles_by_timeframe.get(timeframe_name, 0)))
            if warmup_count:
                rows = db.fetch_all(
                    f"""
                    WITH warmup AS (
                        SELECT timestamp, open, high, low, close, volume
                        FROM {table}
                        WHERE stock_id = %s AND timestamp < %s{session_filter}
                        ORDER BY timestamp DESC
                        LIMIT %s
                    ), replay_window AS (
                        SELECT timestamp, open, high, low, close, volume
                        FROM {table}
                        WHERE stock_id = %s AND timestamp >= %s AND timestamp <= %s{session_filter}
                    )
                    SELECT timestamp, open, high, low, close, volume
                    FROM (
                        SELECT * FROM warmup
                        UNION ALL
                        SELECT * FROM replay_window
                    ) bounded_candles
                    ORDER BY timestamp ASC
                    """,
                    (stock["id"], start_dt, warmup_count, stock["id"], start_dt, end_dt),
                )
            else:
                rows = db.fetch_all(
                    f"""
                    SELECT timestamp, open, high, low, close, volume
                    FROM {table}
                    WHERE stock_id = %s AND timestamp >= %s AND timestamp <= %s{session_filter}
                    ORDER BY timestamp ASC
                    """,
                    (stock["id"], start_dt, end_dt),
                )

        daily_ambiguous = 0
        excluded_out_of_session = 0
        if is_daily:
            rows, daily_quality = normalize_daily_candles(rows)
            daily_ambiguous = daily_quality["skipped_ambiguous_sessions"]
            if daily_quality["duplicate_sessions"]:
                logger.info(
                    "Collapsed {} duplicate daily session(s) for {} during filter backtest loading",
                    daily_quality["duplicate_sessions"],
                    stock["symbol"],
                )
            if daily_ambiguous:
                logger.warning(
                    "Skipped {} conflicting daily session(s) without a unique 15:30 IST candle for {}",
                    daily_ambiguous,
                    stock["symbol"],
                )
            if warmup_candles_by_timeframe is not None:
                warmup_count = max(0, int(warmup_candles_by_timeframe.get(timeframe_name, 0)))
                start_session = start_dt.astimezone(IST).date()
                end_session = end_dt.astimezone(IST).date()
                warmup_rows = [row for row in rows if row["timestamp"].date() < start_session]
                replay_rows = [
                    row for row in rows
                    if start_session <= row["timestamp"].date() <= end_session
                ]
                rows = warmup_rows[-warmup_count:] + replay_rows if warmup_count else replay_rows
        elif timeframe_name in INTRADAY_TIMEFRAMES:
            kept_rows = [row for row in rows if is_regular_session_candle(row["timestamp"])]
            excluded_out_of_session = len(rows) - len(kept_rows)
            rows = kept_rows

        valid_points: list[CandlePoint] = []
        invalid_count = 0
        for row in rows:
            if not is_valid_ohlcv(row["open"], row["high"], row["low"], row["close"], row["volume"]):
                invalid_count += 1
                continue
            valid_points.append(CandlePoint(
                symbol=stock["symbol"],
                timestamp=row["timestamp"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
        candles_by_timeframe[Timeframe(timeframe_name)] = valid_points
        skipped_quality_count = invalid_count + daily_ambiguous + excluded_out_of_session
        if invalid_count:
            logger.warning(
                "Skipped {} malformed {} candles for {} during backtest loading",
                invalid_count,
                timeframe_name,
                stock["symbol"],
            )
        if quality_counts is not None and skipped_quality_count:
            quality_counts[timeframe_name] = (
                quality_counts.get(timeframe_name, 0) + skipped_quality_count
            )
    return db, stock, start_dt, end_dt, candles_by_timeframe


def _run_filter_backtest_expression(
    expression: FilterExpression,
    req: FilterBacktestRequest,
    requirements: dict[str, dict[str, Any]],
    exit_defaults: dict[str, float | int] | None = None,
    execution_timeframe: Timeframe | None = None,
    shared_inputs=None,
    evaluator: FilterEvaluator | None = None,
    charge_config: DeliveryChargeConfig | None = None,
    entry_signal_date_map: dict[str, str] | None = None,
):
    """Load point-in-time candle inputs and run a filter expression.

    The execution timeframe is the clock used for entries, exits, and signal
    evaluation. Other requested timeframes are aligned point-in-time to each
    execution candle, which prevents daily context from being replaced by a
    misleading 200-bar five-minute equivalent.
    """
    if shared_inputs is None:
        db, stock, start_dt, end_dt, candles_by_timeframe = _load_filter_backtest_inputs(
            req,
            requirements,
            execution_timeframe,
        )
    else:
        db, stock, start_dt, end_dt, candles_by_timeframe = shared_inputs

    primary = (
        candles_by_timeframe.get(execution_timeframe)
        if execution_timeframe is not None
        else next(iter(candles_by_timeframe.values()), [])
    ) or next(iter(candles_by_timeframe.values()), [])
    if len(primary) < 2:
        raise HTTPException(status_code=400, detail="Insufficient historical candles for a backtest")

    exit_config = _resolved_exit_config(req, exit_defaults)
    charge_config = charge_config or config_from_store(store("config"))
    config = ProfileBacktestConfig(
        initial_capital=req.initial_capital,
        position_size_pct=req.position_size_pct,
        commission_pct=req.commission_pct,
        charge_config=charge_config,
        slippage_pct=req.slippage_pct,
        stop_loss_pct=float(exit_config["stop_loss_pct"]),
        take_profit_pct=float(exit_config["take_profit_pct"]),
        max_hold_bars=int(exit_config["max_hold_bars"]),
        close_on_filter_fail=req.close_on_filter_fail,
    )
    try:
        result = ProfileBacktester(
            symbol=stock["symbol"],
            candles_by_timeframe=candles_by_timeframe,
            expression=expression,
            config=config,
            start_date=start_dt,
            end_date=end_dt,
            execution_timeframe=execution_timeframe,
            evaluator=evaluator,
        ).run(entry_signal_date_map=entry_signal_date_map)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["company_name"] = _stock_company_name(stock["symbol"], stock.get("name")) or ""
    result["sector"] = _stock_sector_map(db, [stock["id"]]).get(int(stock["id"]))
    return db, stock, start_dt, end_dt, result


def _run_and_persist_filter_preset(
    preset_id: str,
    req: FilterBacktestRequest,
    *,
    shared_inputs=None,
    evaluator: FilterEvaluator | None = None,
    compact_response: bool = False,
    charge_config: DeliveryChargeConfig | None = None,
) -> dict[str, Any]:
    """Run and persist one curated combination.

    This synchronous unit is deliberately safe to execute in a worker thread.
    The batch endpoint uses one worker per preset so CPU-bound backtest work and
    database reads/writes can progress independently.
    """
    import json as _json

    try:
        preset = get_filter_preset(preset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _, requirements, errors = _parse_filter_expression(
        FilterRequirementsRequest(
            mode=EvaluationMode.BACKTEST,
            expression=preset.expression.model_dump(mode="json"),
        )
    )
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    db, stock, start_dt, end_dt, result = _run_filter_backtest_expression(
        preset.expression,
        req,
        requirements,
        execution_timeframe=preset.timeframe,
        shared_inputs=shared_inputs,
        evaluator=evaluator,
        charge_config=charge_config,
        exit_defaults={
            "stop_loss_pct": preset.default_stop_loss_pct,
            "take_profit_pct": preset.default_take_profit_pct,
            "max_hold_bars": preset.max_hold_bars,
        },
    )
    exit_config = _resolved_exit_config(
        req,
        {
            "stop_loss_pct": preset.default_stop_loss_pct,
            "take_profit_pct": preset.default_take_profit_pct,
            "max_hold_bars": preset.max_hold_bars,
        },
    )
    config_json = req.model_dump(mode="json")
    config_json.update(exit_config)
    config_json["execution_timeframe"] = preset.timeframe.value
    config_json["charge_config"] = result.get("charge_config", {})
    run = db.execute_returning(
        """
        INSERT INTO filter_backtest_runs
            (profile_id, version_id, preset_id, preset_name, symbol,
             start_date, end_date, initial_capital, status, config_json, result_json)
        VALUES (NULL, NULL, %s, %s, %s, %s, %s, %s, 'completed', %s, %s)
        RETURNING id
        """,
        (
            preset.preset_id,
            preset.name,
            stock["symbol"],
            start_dt,
            end_dt,
            req.initial_capital,
            _json.dumps(config_json),
            _json.dumps(result),
        ),
    )
    return {
        "run_id": run["id"] if run else None,
        "preset_id": preset.preset_id,
        "preset_name": preset.name,
        "status": "completed",
        "result": _compact_filter_backtest_result(result) if compact_response else result,
    }


def _compact_filter_backtest_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep batch responses small; full trades remain available by run ID."""
    compact = dict(result)
    compact["equity_curve"] = []
    compact["trades"] = []
    return compact


@app.post("/api/filter-backtest-presets/{preset_id}")
def run_filter_backtest_preset(preset_id: str, req: FilterBacktestRequest):
    """Run a curated combination directly; no profile-builder stage is needed."""
    return _run_and_persist_filter_preset(preset_id, req)


@app.post("/api/filter-backtests/batch")
async def run_filter_backtests_batch(req: FilterBacktestBatchRequest):
    """Run curated combinations with shared candles and indicator calculations."""
    presets = build_filter_presets()
    preset_by_id = {preset.preset_id: preset for preset in presets}
    preset_ids = req.preset_ids or list(preset_by_id)
    unknown_ids = [preset_id for preset_id in preset_ids if preset_id not in preset_by_id]
    if unknown_ids:
        raise HTTPException(status_code=404, detail=f"Unknown filter preset(s): {', '.join(unknown_ids)}")

    request_payload = FilterBacktestRequest.model_validate(req.model_dump(exclude={"preset_ids"}))
    # The work is CPU-bound. One background worker keeps the event loop free
    # without making several Python threads duplicate the same indicator work.
    return await asyncio.to_thread(
        _run_filter_backtest_batch_shared,
        preset_ids,
        request_payload,
        preset_by_id,
    )


def _run_filter_backtest_batch_shared(
    preset_ids: list[str],
    request_payload: FilterBacktestRequest,
    preset_by_id: dict[str, Any],
) -> dict[str, Any]:
    """Execute a batch from one candle snapshot and one atomic feature cache."""
    selected_presets = [preset_by_id[preset_id] for preset_id in preset_ids]
    aggregate_requirements: dict[str, dict[str, Any]] = {}
    for preset in selected_presets:
        _, requirements, errors = _parse_filter_expression(
            FilterRequirementsRequest(
                mode=EvaluationMode.BACKTEST,
                expression=preset.expression.model_dump(mode="json"),
            )
        )
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        for timeframe, current in requirements.items():
            existing = aggregate_requirements.setdefault(
                timeframe,
                {
                    "minimum_candles": 0,
                    "warmup": 0,
                    "required_fields": [],
                    "requires_live": False,
                },
            )
            existing["minimum_candles"] = max(existing["minimum_candles"], current["minimum_candles"])
            existing["warmup"] = max(existing["warmup"], current["warmup"])
            existing["required_fields"] = sorted(set(existing["required_fields"]) | set(current["required_fields"]))
            existing["requires_live"] = existing["requires_live"] or current["requires_live"]

    # Every curated preset currently executes on 5m, but include the preset
    # clock explicitly so future combinations cannot accidentally omit it.
    for preset in selected_presets:
        aggregate_requirements.setdefault(preset.timeframe.value, {})

    shared_inputs = _load_filter_backtest_inputs(request_payload, aggregate_requirements)
    candles_by_timeframe = shared_inputs[4]
    atomic_cache = build_atomic_backtest_cache(
        (preset.expression for preset in selected_presets),
        candles_by_timeframe,
        _FILTER_CATALOG,
    )
    evaluator = FilterEvaluator(_FILTER_CATALOG, atomic_cache=atomic_cache)
    charge_config = config_from_store(store("config"))

    results: list[dict[str, Any]] = []
    for preset_id in preset_ids:
        try:
            results.append(
                _run_and_persist_filter_preset(
                    preset_id,
                    request_payload,
                    shared_inputs=shared_inputs,
                    evaluator=evaluator,
                    compact_response=True,
                    charge_config=charge_config,
                )
            )
        except Exception as outcome:
            detail = outcome.detail if isinstance(outcome, HTTPException) else str(outcome)
            results.append(
                {
                    "run_id": None,
                    "preset_id": preset_id,
                    "preset_name": preset_by_id[preset_id].name,
                    "status": "failed",
                    "result": None,
                    "error": detail if isinstance(detail, str) else str(detail),
                }
            )

    completed = sum(row.get("result") is not None for row in results)
    return {
        "status": "completed" if completed == len(results) else "partial",
        "completed": completed,
        "total": len(results),
        "results": results,
    }


def _number(value: Any, default: float = 0.0) -> float:
    """Return a finite numeric value without letting a malformed result skew ranks."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _classify_watchlist_market_behavior(metrics: dict[str, Any]) -> dict[str, str]:
    """Describe observed daily-price behaviour; this is not a price forecast."""
    observations = int(metrics.get("observations", 0))
    total_return = _number(metrics.get("total_return_pct"))
    efficiency = _number(metrics.get("trend_efficiency_pct"))
    volatility = _number(metrics.get("annualized_volatility_pct"))

    if observations < 30:
        return {
            "id": "insufficient_daily_data",
            "label": "Insufficient daily data",
            "description": "Fewer than 30 daily candles were available in the selected period.",
        }
    if abs(total_return) <= 12 and efficiency < 35 and volatility < 45:
        return {
            "id": "sideways",
            "label": "Sideways",
            "description": "Small net move with no persistent directional path.",
        }
    if total_return >= 75:
        return {
            "id": "strong_uptrend",
            "label": "Strong uptrend",
            "description": "Large positive one-year return; the exact smoothness is shown by trend efficiency.",
        }
    if total_return >= 20:
        return {
            "id": "uptrend",
            "label": "Uptrend",
            "description": "Positive directional one-year return.",
        }
    if total_return <= -45:
        return {
            "id": "strong_downtrend",
            "label": "Strong downtrend",
            "description": "Large negative one-year return.",
        }
    if total_return <= -20:
        return {
            "id": "downtrend",
            "label": "Downtrend",
            "description": "Negative directional one-year return.",
        }
    if volatility >= 45:
        return {
            "id": "volatile_range",
            "label": "Volatile range",
            "description": "Large daily swings without a qualifying sustained trend.",
        }
    return {
        "id": "mixed",
        "label": "Mixed",
        "description": "Neither a clear sideways market nor a sustained directional move.",
    }


def _load_watchlist_market_profile(
    db: Database,
    stock: dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
) -> dict[str, Any]:
    """Calculate an explainable one-year price regime from daily candles only."""
    rows = db.fetch_all(
        """
        SELECT timestamp, high, low, close
        FROM candles_1d
        WHERE stock_id = %s AND timestamp >= %s AND timestamp <= %s
        ORDER BY timestamp ASC
        """,
        (stock["id"], start_dt, end_dt),
    )
    rows, daily_quality = normalize_daily_candles(rows)
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily session(s) while calculating the watchlist market profile for {}",
            daily_quality["skipped_ambiguous_sessions"],
            stock["symbol"],
        )
    valid_rows = [(row, _number(row.get("close"))) for row in rows]
    valid_rows = [(row, close) for row, close in valid_rows if close > 0]
    rows = [row for row, _ in valid_rows]
    closes = [close for _, close in valid_rows]
    observations = len(closes)
    if observations < 2:
        metrics = {
            "observations": observations,
            "first_timestamp": rows[0]["timestamp"] if rows else None,
            "last_timestamp": rows[-1]["timestamp"] if rows else None,
            "total_return_pct": None,
            "annualized_volatility_pct": None,
            "max_drawdown_pct": None,
            "trend_efficiency_pct": None,
            "range_pct": None,
        }
        metrics["behavior"] = _classify_watchlist_market_behavior(metrics)
        return metrics

    first_close, last_close = closes[0], closes[-1]
    daily_returns = [closes[index] / closes[index - 1] - 1 for index in range(1, observations)]
    mean_return = sum(daily_returns) / len(daily_returns)
    variance = (
        sum((daily_return - mean_return) ** 2 for daily_return in daily_returns)
        / max(len(daily_returns) - 1, 1)
    )
    annualized_volatility = math.sqrt(variance) * math.sqrt(252) * 100
    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        max_drawdown = max(max_drawdown, (1 - close / peak) * 100)
    path_distance = sum(abs(closes[index] - closes[index - 1]) for index in range(1, observations))
    trend_efficiency = abs(last_close - first_close) / path_distance * 100 if path_distance else 0.0
    highs = [_number(row.get("high"), close) for row, close in zip(rows, closes)]
    lows = [_number(row.get("low"), close) for row, close in zip(rows, closes)]
    lowest_low = min(lows)
    range_pct = (max(highs) / lowest_low - 1) * 100 if lowest_low > 0 else 0.0
    metrics = {
        "observations": observations,
        "first_timestamp": rows[0]["timestamp"],
        "last_timestamp": rows[-1]["timestamp"],
        "first_close": round(first_close, 4),
        "last_close": round(last_close, 4),
        "total_return_pct": round((last_close / first_close - 1) * 100, 2),
        "annualized_volatility_pct": round(annualized_volatility, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "trend_efficiency_pct": round(trend_efficiency, 2),
        "range_pct": round(range_pct, 2),
    }
    metrics["behavior"] = _classify_watchlist_market_behavior(metrics)
    return metrics


def _watchlist_combination_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Strip persisted payloads down to the metrics useful in a multi-stock comparison."""
    result = row.get("result")
    if not result:
        return {
            "preset_id": row["preset_id"],
            "preset_name": row["preset_name"],
            "run_id": row.get("run_id"),
            "status": row.get("status", "failed"),
            "error": row.get("error"),
        }
    return {
        "preset_id": row["preset_id"],
        "preset_name": row["preset_name"],
        "run_id": row.get("run_id"),
        "status": row.get("status", "completed"),
        "execution_timeframe": result.get("execution_timeframe"),
        "start_date": result.get("start_date"),
        "end_date": result.get("end_date"),
        "total_return_pct": round(_number(result.get("total_return_pct")), 4),
        "final_capital": round(_number(result.get("final_capital")), 2),
        "total_charges": round(_number(result.get("total_charges")), 2),
        "max_drawdown_pct": round(_number(result.get("max_drawdown_pct")), 4),
        "sharpe_ratio": round(_number(result.get("sharpe_ratio")), 4),
        "win_rate_pct": round(_number(result.get("win_rate_pct")), 4),
        "total_trades": int(_number(result.get("total_trades"))),
    }


def _best_watchlist_combination(combinations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rank by return; ties prefer lower drawdown, then a stronger Sharpe ratio."""
    completed = [row for row in combinations if row.get("status") == "completed"]
    if not completed:
        return None
    return min(
        completed,
        key=lambda row: (
            -_number(row.get("total_return_pct")),
            _number(row.get("max_drawdown_pct"), 1_000_000),
            -_number(row.get("sharpe_ratio")),
        ),
    )


def _filter_leaderboard(
    stock_results: list[dict[str, Any]],
    *,
    portfolio_initial_capital: float | None = None,
    portfolio_stock_count: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate independent backtests and, when requested, an equal-weight portfolio."""
    aggregates: dict[str, dict[str, Any]] = {}
    for stock_result in stock_results:
        for combination in stock_result.get("combinations", []):
            if combination.get("status") != "completed":
                continue
            bucket = aggregates.setdefault(
                combination["preset_id"],
                {
                    "preset_id": combination["preset_id"],
                    "preset_name": combination["preset_name"],
                    "returns": [],
                    "drawdowns": [],
                    "sharpes": [],
                    "trade_counts": [],
                    "positive_stock_count": 0,
                },
            )
            total_return = _number(combination.get("total_return_pct"))
            bucket["returns"].append(total_return)
            bucket["drawdowns"].append(_number(combination.get("max_drawdown_pct")))
            bucket["sharpes"].append(_number(combination.get("sharpe_ratio")))
            bucket["trade_counts"].append(int(_number(combination.get("total_trades"))))
            bucket["positive_stock_count"] += int(total_return > 0)

    leaderboard = []
    for bucket in aggregates.values():
        returns = sorted(bucket.pop("returns"))
        count = len(returns)
        median_return = returns[count // 2] if count % 2 else (returns[count // 2 - 1] + returns[count // 2]) / 2
        row = {
            "preset_id": bucket["preset_id"],
            "preset_name": bucket["preset_name"],
            "stock_count": count,
            "average_return_pct": round(sum(returns) / count, 2),
            "median_return_pct": round(median_return, 2),
            "average_max_drawdown_pct": round(sum(bucket.pop("drawdowns")) / count, 2),
            "average_sharpe_ratio": round(sum(bucket.pop("sharpes")) / count, 3),
            "average_trade_count": round(sum(bucket.pop("trade_counts")) / count, 1),
            "positive_stock_count": bucket["positive_stock_count"],
        }
        if portfolio_initial_capital is not None and portfolio_stock_count:
            # Equal allocation makes the aggregate return the sum of each
            # completed stock's return divided by every selected stock. Any
            # stock without a completed result simply retains its allocation
            # as cash instead of inflating or deflating the portfolio result.
            portfolio_return_pct = sum(returns) / portfolio_stock_count
            row.update(
                {
                    "portfolio_initial_capital": round(portfolio_initial_capital, 2),
                    "portfolio_return_pct": round(portfolio_return_pct, 2),
                    "portfolio_final_capital": round(
                        portfolio_initial_capital * (1 + portfolio_return_pct / 100),
                        2,
                    ),
                }
            )
        leaderboard.append(row)
    return sorted(
        leaderboard,
        key=lambda row: (
            -_number(row["average_return_pct"]),
            -_number(row["median_return_pct"]),
            _number(row["average_max_drawdown_pct"], 1_000_000),
            -_number(row["average_sharpe_ratio"]),
        ),
    )


def _watchlist_buy_and_hold_summary(stock_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate the shared, equal-weight buy-and-hold benchmark for the watchlist."""
    returns: list[float] = []
    for stock_result in stock_results:
        market_profile = stock_result.get("market_profile") or {}
        value = market_profile.get("total_return_pct")
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            returns.append(parsed)
    return {
        "average_watchlist_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "watchlist_return_stock_count": len(returns),
    }


def _watchlist_regime_recommendations(stock_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for stock_result in stock_results:
        behavior = stock_result.get("market_profile", {}).get("behavior", {})
        grouped.setdefault(behavior.get("id", "mixed"), []).append(stock_result)

    recommendations = []
    for behavior_id, stocks in grouped.items():
        behavior = stocks[0].get("market_profile", {}).get("behavior", {})
        leaderboard = _filter_leaderboard(stocks)
        recommendations.append(
            {
                "regime_id": behavior_id,
                "regime_label": behavior.get("label", "Mixed"),
                "description": behavior.get("description", ""),
                "stock_count": len(stocks),
                "symbols": [stock["symbol"] for stock in stocks],
                "recommended_filter": leaderboard[0] if leaderboard else None,
                "leaderboard": leaderboard,
            }
        )
    return sorted(recommendations, key=lambda row: (-row["stock_count"], row["regime_label"]))


def _watchlist_analysis_stock(
    symbol: str,
    request: WatchlistFilterAnalysisRequest,
    preset_ids: list[str],
    preset_by_id: dict[str, Any],
) -> dict[str, Any]:
    """Run one stock as a shared-cache batch, then describe its daily regime."""
    backtest_request = FilterBacktestRequest(
        symbol=symbol,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
    )
    db = get_db()
    stock = _resolve_stock_id(db, symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {symbol} not found")
    start_dt, end_dt = _backtest_date_bounds(backtest_request)
    # The daily profile is calculated before the batch to make failures in the
    # strategy layer distinguishable from missing daily classification data.
    market_profile = _load_watchlist_market_profile(db, stock, start_dt, end_dt)
    batch = _run_filter_backtest_batch_shared(preset_ids, backtest_request, preset_by_id)
    combinations = [_watchlist_combination_summary(row) for row in batch["results"]]
    return {
        "symbol": stock["symbol"],
        "stock_name": stock.get("name") or stock["symbol"],
        "company_name": _stock_company_name(stock["symbol"], stock.get("name")) or "",
        "sector": _stock_sector_map(db, [stock["id"]]).get(int(stock["id"])),
        "status": batch["status"],
        "market_profile": market_profile,
        "best_combination": _best_watchlist_combination(combinations),
        "combinations": combinations,
    }


def _watchlist_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """Return only JSON-safe, client-facing job state while a worker updates it."""
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        # Keep counters as integers in the API contract. Decimal-like values
        # from the database still become ordinary JSON numbers.
        if hasattr(value, "__float__") and not isinstance(value, (str, bytes, bool, int, float)):
            return float(value)
        return value

    return convert({key: value for key, value in job.items() if key not in {"_created_sequence"}})


def _run_watchlist_analysis_job(
    job_id: str,
    request: WatchlistFilterAnalysisRequest,
    symbols: list[str],
    preset_ids: list[str],
    preset_by_id: dict[str, Any],
) -> None:
    with _watchlist_analysis_lock:
        job = _watchlist_analysis_jobs[job_id]
        if job["status"] == "cancelled":
            return
        job.update({"status": "running", "started_at": datetime.now(timezone.utc)})

    stock_results: list[dict[str, Any]] = []
    executor: ProcessPoolExecutor | None = None
    try:
        executor = ProcessPoolExecutor(
            max_workers=min(_MAX_WATCHLIST_ANALYSIS_WORKERS, len(symbols)),
            # ``spawn`` gives each worker a clean interpreter and, crucially,
            # never inherits a psycopg connection from the API process.
            mp_context=get_context("spawn"),
            initializer=_initialize_cpu_analysis_worker,
        )
        with _watchlist_analysis_lock:
            # A new request can supersede this job while the pool is starting.
            # In that case do not publish the executor or submit any work.
            if _watchlist_analysis_jobs[job_id]["status"] == "cancelled":
                executor.shutdown(wait=False, cancel_futures=True)
                return
            _watchlist_analysis_executors[job_id] = executor

        futures = {}
        for symbol in symbols:
            with _watchlist_analysis_lock:
                if _watchlist_analysis_jobs[job_id]["status"] == "cancelled":
                    return
            futures[executor.submit(_watchlist_analysis_stock, symbol, request, preset_ids, preset_by_id)] = symbol

        for future in as_completed(futures):
            with _watchlist_analysis_lock:
                if _watchlist_analysis_jobs[job_id]["status"] == "cancelled":
                    return
            symbol = futures[future]
            try:
                stock_result = future.result()
            except Exception as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                stock_result = {
                    "symbol": symbol,
                    "stock_name": symbol,
                    "company_name": _stock_company_name(symbol) or "",
                    "sector": None,
                    "status": "failed",
                    "error": detail if isinstance(detail, str) else str(detail),
                    "market_profile": {},
                    "best_combination": None,
                    "combinations": [],
                }
            stock_results.append(stock_result)
            completed_backtests = len(stock_result.get("combinations", []))
            successful_backtests = sum(
                item.get("status") == "completed" for item in stock_result.get("combinations", [])
            )
            with _watchlist_analysis_lock:
                job = _watchlist_analysis_jobs[job_id]
                if job["status"] == "cancelled":
                    return
                job["stock_results"] = list(stock_results)
                job["completed_stocks"] += 1
                job["completed_backtests"] += completed_backtests
                job["successful_backtests"] += successful_backtests
                if stock_result.get("status") == "failed":
                    job["failed_stocks"] += 1

        order = {symbol: index for index, symbol in enumerate(symbols)}
        stock_results.sort(key=lambda item: order.get(item["symbol"], len(order)))
        successful_stocks = [row for row in stock_results if row.get("combinations")]
        watchlist_return = _watchlist_buy_and_hold_summary(stock_results)
        with _watchlist_analysis_lock:
            job = _watchlist_analysis_jobs[job_id]
            if job["status"] == "cancelled":
                return
            portfolio_initial_capital = job.get("portfolio_initial_capital")
        leaderboard = _filter_leaderboard(
            successful_stocks,
            portfolio_initial_capital=_number(portfolio_initial_capital) if portfolio_initial_capital is not None else None,
            portfolio_stock_count=len(symbols),
        )
        with _watchlist_analysis_lock:
            job = _watchlist_analysis_jobs[job_id]
            if job["status"] == "cancelled":
                return
            job.update(
                {
                    "status": "completed" if job["successful_backtests"] == job["total_backtests"] else "partial",
                    "completed_at": datetime.now(timezone.utc),
                    "stock_results": stock_results,
                    "overall_leaderboard": leaderboard,
                    "regime_recommendations": _watchlist_regime_recommendations(successful_stocks),
                    **watchlist_return,
                }
            )
    except Exception as exc:
        with _watchlist_analysis_lock:
            job = _watchlist_analysis_jobs[job_id]
            if job["status"] != "cancelled":
                logger.exception("Watchlist filter analysis job failed")
                job.update(
                    {
                        "status": "failed",
                        "completed_at": datetime.now(timezone.utc),
                        "error": str(exc),
                    }
                )
    finally:
        if executor is not None:
            with _watchlist_analysis_lock:
                owns_executor = _watchlist_analysis_executors.get(job_id) is executor
                if owns_executor:
                    del _watchlist_analysis_executors[job_id]
            if owns_executor:
                executor.shutdown(wait=True, cancel_futures=False)


def _resolve_watchlist_analysis_symbols(
    request: WatchlistFilterAnalysisRequest,
) -> tuple[list[str], dict[str, Any]]:
    db = get_db()
    requested_symbols = list(request.symbols)
    source: dict[str, Any] = {"type": "manual", "name": "Pasted symbols"}
    if request.watchlist_id is not None:
        watchlist = db.fetch_one(
            "SELECT id, name FROM watchlists WHERE id = %s AND is_active = TRUE",
            (request.watchlist_id,),
        )
        if not watchlist:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        watchlist_rows = db.fetch_all(
            f"""
            SELECT s.symbol
            FROM watchlist_items wi
            JOIN stocks s ON s.id = wi.stock_id
            WHERE wi.watchlist_id = %s AND {equity_universe_sql('s')}
            ORDER BY wi.priority DESC, s.symbol
            """,
            (request.watchlist_id,),
        )
        requested_symbols.extend(row["symbol"] for row in watchlist_rows)
        source = {"type": "watchlist", "id": watchlist["id"], "name": watchlist["name"]}

    normalized = [symbol.strip() for symbol in requested_symbols if symbol and symbol.strip()]
    if not normalized:
        raise HTTPException(status_code=422, detail="Choose a watchlist or provide at least one stock symbol")
    resolved_symbols: list[str] = []
    unknown_symbols: list[str] = []
    seen: set[str] = set()
    for symbol in normalized:
        stock = _resolve_stock_id(db, symbol)
        if not stock or not is_equity_stock(stock):
            unknown_symbols.append(symbol.upper())
            continue
        canonical_symbol = stock["symbol"]
        if canonical_symbol not in seen:
            seen.add(canonical_symbol)
            resolved_symbols.append(canonical_symbol)
    if unknown_symbols:
        raise HTTPException(status_code=422, detail=f"Unknown stock symbol(s): {', '.join(unknown_symbols)}")
    if request.initial_capital <= 0:
        raise HTTPException(status_code=422, detail="Initial capital must be greater than 0")
    # Validate dates before returning a job id.  We use a real resolved symbol
    # solely because the single-stock request owns the common date contract.
    _backtest_date_bounds(
        FilterBacktestRequest(
            symbol=resolved_symbols[0],
            start_date=request.start_date,
            end_date=request.end_date,
            initial_capital=request.initial_capital,
        )
    )
    return resolved_symbols, source


@app.post("/api/filter-backtests/watchlist-analysis")
def start_watchlist_filter_analysis(request: WatchlistFilterAnalysisRequest):
    """Start a progress-reporting comparison of every requested stock and preset."""
    preset_by_id = {preset.preset_id: preset for preset in build_filter_presets()}
    preset_ids = request.preset_ids or list(preset_by_id)
    unknown_ids = [preset_id for preset_id in preset_ids if preset_id not in preset_by_id]
    if unknown_ids:
        raise HTTPException(status_code=404, detail=f"Unknown filter preset(s): {', '.join(unknown_ids)}")
    symbols, source = _resolve_watchlist_analysis_symbols(request)
    capital_per_stock = request.initial_capital / len(symbols)
    # The strategy runs remain independent, but each run now receives its
    # equal share of the user-supplied portfolio fund. Percentage returns and
    # trade decisions are unchanged; absolute result values become truthful.
    per_stock_request = request.model_copy(update={"initial_capital": capital_per_stock})

    superseded_executor: ProcessPoolExecutor | None = None
    with _watchlist_analysis_lock:
        active_job_id = next(
            (
                known_job_id
                for known_job_id, job in _watchlist_analysis_jobs.items()
                if job["status"] in {"queued", "running"}
            ),
            None,
        )
        if active_job_id is not None:
            active_job = _watchlist_analysis_jobs[active_job_id]
            active_job.update(
                {
                    "status": "cancelled",
                    "completed_at": datetime.now(timezone.utc),
                    "error": "Superseded by a newer watchlist analysis request.",
                }
            )
            superseded_executor = _watchlist_analysis_executors.pop(active_job_id, None)
        while len(_watchlist_analysis_jobs) >= _MAX_WATCHLIST_ANALYSIS_JOBS:
            # Keep the just-cancelled job record until its worker observes the
            # cancellation.  Its thread still uses the record to avoid
            # publishing stale results while it unwinds.
            removable_job_ids = [
                known_job_id
                for known_job_id in _watchlist_analysis_jobs
                if known_job_id != active_job_id
            ]
            oldest_job_id = min(
                removable_job_ids,
                key=lambda key: _watchlist_analysis_jobs[key]["created_at"],
            )
            del _watchlist_analysis_jobs[oldest_job_id]
        job_id = uuid.uuid4().hex
        _watchlist_analysis_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc),
            "started_at": None,
            "completed_at": None,
            "source": source,
            "symbols": symbols,
            "portfolio_initial_capital": request.initial_capital,
            "capital_per_stock": capital_per_stock,
            "preset_count": len(preset_ids),
            "total_stocks": len(symbols),
            "completed_stocks": 0,
            "failed_stocks": 0,
            "total_backtests": len(symbols) * len(preset_ids),
            "completed_backtests": 0,
            "successful_backtests": 0,
            "stock_results": [],
            "overall_leaderboard": [],
            "regime_recommendations": [],
            "average_watchlist_return_pct": None,
            "watchlist_return_stock_count": 0,
            "error": None,
        }
        snapshot = _watchlist_job_snapshot(_watchlist_analysis_jobs[job_id])

    if superseded_executor is not None:
        _terminate_watchlist_analysis_executor(superseded_executor)

    Thread(
        target=_run_watchlist_analysis_job,
        args=(job_id, per_stock_request, symbols, preset_ids, preset_by_id),
        name=f"watchlist-filter-analysis-{job_id[:8]}",
        daemon=True,
    ).start()
    return snapshot


@app.get("/api/filter-backtests/watchlist-analysis/{job_id}")
def get_watchlist_filter_analysis(job_id: str):
    """Read live progress or final recommendations for a watchlist analysis."""
    with _watchlist_analysis_lock:
        job = _watchlist_analysis_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Watchlist analysis was not found or has expired")
        return _watchlist_job_snapshot(job)


@app.post("/api/filter-profiles/{profile_id}/backtest")
def backtest_filter_profile(profile_id: int, req: FilterBacktestRequest):
    """Backtest the selected immutable profile version without look-ahead."""
    import json as _json

    profile_store = store("filter_profile")
    version = profile_store.get_version(profile_id)
    if not version:
        raise HTTPException(status_code=404, detail="Filter profile version not found")
    try:
        expression = FilterExpression.model_validate(version.get("expression_json", {}))
        _, requirements, errors = _parse_filter_expression(
            FilterRequirementsRequest(
                mode=EvaluationMode.BACKTEST,
                expression=expression.model_dump(mode="json"),
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    def boundary(value: str, *, end: bool = False) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if end and len(value) == 10:
            parsed += timedelta(days=1) - timedelta(microseconds=1)
        return parsed

    try:
        start_dt = boundary(req.start_date)
        end_dt = boundary(req.end_date, end=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD or ISO timestamps") from exc
    if end_dt < start_dt:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")

    db = get_db()
    stock = _resolve_stock_id(db, req.symbol)
    if not stock:
        raise HTTPException(status_code=404, detail=f"Stock {req.symbol} not found")

    # Backtests execute on completed five-minute bars.  A saved profile may
    # contain only daily conditions, so always load the execution series in
    # addition to the profile's declared context timeframes.
    execution_timeframe = Timeframe.FIVE_MINUTE
    candles_by_timeframe: dict[Timeframe, list[dict[str, Any]]] = {}
    required_timeframes = set(requirements) | {execution_timeframe.value}
    for timeframe_name in required_timeframes:
        table = _PREVIEW_TABLES[timeframe_name]
        rows = db.fetch_all(
            f"""
            SELECT timestamp, open, high, low, close, volume
            FROM {table}
            WHERE stock_id = %s AND timestamp <= %s
            ORDER BY timestamp ASC
            """,
            (stock["id"], end_dt),
        )
        candles_by_timeframe[Timeframe(timeframe_name)] = [
            {
                "symbol": stock["symbol"],
                "timestamp": row["timestamp"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for row in rows
        ]

    primary = candles_by_timeframe.get(execution_timeframe) or next(iter(candles_by_timeframe.values()), [])
    if len(primary) < 2:
        raise HTTPException(status_code=400, detail="Insufficient historical candles for a backtest")

    exit_config = _resolved_exit_config(req)
    charge_config = config_from_store(store("config"))
    config = ProfileBacktestConfig(
        initial_capital=req.initial_capital,
        position_size_pct=req.position_size_pct,
        commission_pct=req.commission_pct,
        charge_config=charge_config,
        slippage_pct=req.slippage_pct,
        stop_loss_pct=float(exit_config["stop_loss_pct"]),
        take_profit_pct=float(exit_config["take_profit_pct"]),
        max_hold_bars=int(exit_config["max_hold_bars"]),
        close_on_filter_fail=req.close_on_filter_fail,
    )
    try:
        result = ProfileBacktester(
            symbol=stock["symbol"],
            candles_by_timeframe=candles_by_timeframe,
            expression=expression,
            config=config,
            start_date=start_dt,
            end_date=end_dt,
            execution_timeframe=execution_timeframe,
        ).run()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["company_name"] = _stock_company_name(stock["symbol"], stock.get("name")) or ""
    result["sector"] = _stock_sector_map(db, [stock["id"]]).get(int(stock["id"]))

    config_json = req.model_dump(mode="json")
    config_json.update(exit_config)
    config_json["execution_timeframe"] = execution_timeframe.value
    config_json["charge_config"] = result.get("charge_config", {})
    run = db.execute_returning(
        """
        INSERT INTO filter_backtest_runs
            (profile_id, version_id, symbol, start_date, end_date,
             initial_capital, status, config_json, result_json)
        VALUES (%s, %s, %s, %s, %s, %s, 'completed', %s, %s)
        RETURNING id
        """,
        (
            profile_id,
            version.get("id"),
            stock["symbol"],
            start_dt,
            end_dt,
            req.initial_capital,
            _json.dumps(config_json),
            _json.dumps(result),
        ),
    )
    return {
        "run_id": run["id"] if run else None,
        "profile_id": profile_id,
        "version_id": version.get("id"),
        "status": "completed",
        "result": result,
    }


@app.get("/api/filter-backtests/{run_id}")
def get_filter_backtest(run_id: int):
    db = get_db()
    row = db.fetch_one(
        """
        SELECT id, profile_id, version_id, preset_id, preset_name,
               symbol, start_date, end_date,
               initial_capital, status, config_json, result_json,
               error_message, created_at
        FROM filter_backtest_runs
        WHERE id = %s
        """,
        (run_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Filter backtest not found")
    serialized = _json_profile(row)
    serialized["config"] = serialized.pop("config_json", {}) or {}
    serialized["result"] = serialized.pop("result_json", {}) or {}
    stock = _resolve_stock_id(db, str(serialized.get("symbol") or ""))
    if stock:
        company_name = _stock_company_name(stock["symbol"], stock.get("name")) or ""
        sector = _stock_sector_map(db, [stock["id"]]).get(int(stock["id"]))
        serialized["company_name"] = company_name
        serialized["sector"] = sector
        if isinstance(serialized["result"], dict):
            serialized["result"].setdefault("company_name", company_name)
            serialized["result"].setdefault("sector", sector)
    return {"run": serialized}


@app.delete("/api/filter-backtests/{run_id}")
def delete_filter_backtest(run_id: int):
    affected = get_db().execute("DELETE FROM filter_backtest_runs WHERE id = %s", (run_id,))
    if affected == 0:
        raise HTTPException(status_code=404, detail="Filter backtest not found")
    return {"deleted": True, "run_id": run_id}


@app.get("/api/trendlines")
def list_trendlines(symbol: str, interval: str = "1d"):
    return {"trendlines": [_json_profile(row) for row in store("trendline").list(symbol, interval)]}


@app.post("/api/trendlines")
def create_trendline(req: TrendlineCreateRequest):
    try:
        trendline = store("trendline").create(
            req.symbol,
            req.interval,
            req.points,
            trendline_type=req.trendline_type,
            source=req.source,
            name=req.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"trendline": _json_profile(trendline)}


@app.patch("/api/trendlines/{trendline_id}")
def update_trendline(trendline_id: int, req: TrendlineUpdateRequest):
    try:
        trendline = store("trendline").update(trendline_id, req.points, req.active)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not trendline:
        raise HTTPException(status_code=404, detail="Trendline not found")
    return {"trendline": _json_profile(trendline)}


@app.delete("/api/trendlines/{trendline_id}")
def delete_trendline(trendline_id: int):
    store("trendline").delete(trendline_id)
    return {"deleted": True, "id": trendline_id}


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
    rows, daily_quality = normalize_daily_candles(rows)
    if daily_quality["skipped_ambiguous_sessions"]:
        logger.warning(
            "Skipped {} ambiguous daily session(s) for {} in the standard backtest",
            daily_quality["skipped_ambiguous_sessions"],
            stock["symbol"],
        )

    minimum_candles = 150 if req.algorithm == "swing_volume_profile_pullback" else 201
    if len(rows) < minimum_candles:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient data: {len(rows)} candles found (need 150+ for swing-profile lookback)"
                if req.algorithm == "swing_volume_profile_pullback"
                else f"Insufficient data: {len(rows)} candles found (need 201+ for SMA200 warmup)"
            ),
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
        charge_config = config_from_store(store("config"))
        bt = Backtester(
            candles=candles,
            algorithm=req.algorithm,
            symbol=stock["symbol"],
            initial_capital=req.initial_capital,
            charge_config=charge_config,
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
            total_charges = %s, equity_curve = %s, config = %s,
            start_date = %s, end_date = %s
        WHERE id = %s
        """,
        (
            result.total_return_pct, result.cagr_pct, result.max_drawdown_pct,
            result.sharpe_ratio, result.win_rate_pct, result.total_trades,
            result.winning_trades, result.losing_trades,
            result.avg_win_pct, result.avg_loss_pct, result.profit_factor,
            result.avg_hold_days, result.final_capital, result.total_charges,
            _json.dumps(eq),
            _json.dumps({"charge_config": charge_config.to_dict()}),
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
                 quantity, pnl, pnl_pct, exit_reason, hold_days, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, t.symbol, str(t.entry_date),
                str(t.exit_date) if t.exit_date else None,
                t.entry_price, t.exit_price, t.quantity,
                t.pnl, t.pnl_pct, t.exit_reason, t.hold_days,
                _json.dumps(t.metadata),
            ),
        )

    return {"run_id": run_id, "status": "completed"}


@app.get("/api/backtest/history")
def get_backtest_history(
    section: str = Query("algorithms"),
    limit: int = Query(50, ge=1, le=200),
):
    """List history for the selected backtest section."""
    if section not in {"algorithms", "filters"}:
        raise HTTPException(status_code=422, detail="section must be algorithms or filters")

    db = get_db()

    if section == "filters":
        import json as _json

        rows = db.fetch_all(
            """
            SELECT fbr.id, fbr.preset_id, fbr.preset_name,
                   fp.name AS profile_name, fbr.symbol, fbr.start_date,
                   fbr.end_date, fbr.initial_capital, fbr.status,
                   fbr.result_json, fbr.created_at
            FROM filter_backtest_runs fbr
            LEFT JOIN filter_profiles fp ON fp.id = fbr.profile_id
            ORDER BY fbr.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        runs = []
        for row in rows:
            result = row.get("result_json") or {}
            if isinstance(result, str):
                try:
                    result = _json.loads(result)
                except _json.JSONDecodeError:
                    result = {}
            runs.append(
                _serialize_row(
                    {
                        "id": row["id"],
                        "backtest_type": "filter",
                        "display_name": row.get("preset_name") or row.get("profile_name") or "Filter combination",
                        "stock_symbol": row["symbol"],
                        "stock_name": "",
                        "start_date": row["start_date"],
                        "end_date": row["end_date"],
                        "initial_capital": row["initial_capital"],
                        "status": row["status"],
                        "total_return_pct": result.get("total_return_pct"),
                        "max_drawdown_pct": result.get("max_drawdown_pct"),
                        "sharpe_ratio": result.get("sharpe_ratio"),
                        "win_rate_pct": result.get("win_rate_pct"),
                        "total_trades": result.get("total_trades"),
                        "created_at": row["created_at"],
                    }
                )
            )
        _attach_stock_display_metadata_by_symbol(db, runs, "stock_symbol")
        return {"runs": runs}

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
    runs = []
    for row in rows:
        serialized = _serialize_row(row)
        serialized["backtest_type"] = "algorithm"
        serialized["display_name"] = row["algorithm_name"]
        runs.append(serialized)
    _attach_stock_display_metadata_by_symbol(db, runs, "stock_symbol")
    return {"runs": runs}


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
    _attach_stock_display_metadata_by_symbol(db, [result], "stock_symbol")
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


# ── Query-selected stocks → technical setup backtests ─────────────────────

_fundamental_backtest_lock = Lock()
_fundamental_backtest_jobs: dict[str, dict[str, Any]] = {}
_MAX_FUNDAMENTAL_BACKTEST_JOBS = 20
_MAX_FUNDAMENTAL_BACKTEST_WORKERS = _MAX_WATCHLIST_ANALYSIS_WORKERS
FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK = 100_000.0
_fundamental_executor_lock = Lock()
_fundamental_backtest_executor: ProcessPoolExecutor | None = None


def _get_fundamental_backtest_executor() -> ProcessPoolExecutor:
    """Share a bounded spawn pool across query backtests to avoid oversubscription."""
    global _fundamental_backtest_executor
    with _fundamental_executor_lock:
        if _fundamental_backtest_executor is None:
            _fundamental_backtest_executor = ProcessPoolExecutor(
                max_workers=_MAX_FUNDAMENTAL_BACKTEST_WORKERS,
                mp_context=get_context("spawn"),
                initializer=_initialize_cpu_analysis_worker,
            )
        return _fundamental_backtest_executor


class FundamentalQueryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    rules: list[dict[str, Any]] = Field(min_length=1, max_length=16)


class FundamentalQueryPreviewRequest(BaseModel):
    rules: list[dict[str, Any]] = Field(min_length=1, max_length=16)
    as_of: date | None = None


class FundamentalQueryBacktestRequest(BaseModel):
    query_id: int
    start_date: date
    end_date: date
    # The frontend sends the exact stocks returned by the user's query preview.
    # Older clients may omit this list; those runs select the query universe
    # once at universe_as_of (or today), then keep it fixed for the full test.
    selected_stock_ids: list[int] | None = Field(default=None, max_length=5000)
    universe_as_of: date | None = None
    setup_ids: list[str] | None = None
    target_r: float = Field(default=2.0, ge=0.5, le=10)
    max_hold_bars: int = Field(default=1500, ge=1, le=7500)
    stop_buffer_pct: float = Field(default=0.5, ge=0, le=10)


def _canonical_fundamental_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return validate_rules(rules)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _fundamental_query_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **_serialize_row(row),
        "query_text": render_query(row["rules"]),
    }


@app.get("/api/fundamental-setup-presets")
def list_fundamental_setup_presets():
    """Return built-in setup definitions and their common 5-minute execution interval."""
    return {
        "setups": [
            {"id": setup_id, "candle_interval": FUNDAMENTAL_SETUP_CANDLE_INTERVAL, **setup}
            for setup_id, setup in SETUPS.items()
        ]
    }


def _saved_fundamental_query(query_id: int) -> dict[str, Any]:
    row = get_db().fetch_one(
        "SELECT id, name, version, rules, created_at, updated_at "
        "FROM fundamental_queries WHERE id = %s AND is_active = TRUE",
        (query_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Fundamental query not found")
    return _fundamental_query_row(row)


@app.get("/api/fundamental-queries")
def list_fundamental_queries():
    rows = get_db().fetch_all(
        "SELECT id, name, version, rules, created_at, updated_at "
        "FROM fundamental_queries WHERE is_active = TRUE ORDER BY updated_at DESC, id DESC"
    )
    return {"queries": [_fundamental_query_row(row) for row in rows]}


@app.post("/api/fundamental-queries")
def create_fundamental_query(request: FundamentalQueryRequest):
    rules = _canonical_fundamental_rules(request.rules)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Query name cannot be blank")
    row = get_db().execute_returning(
        "INSERT INTO fundamental_queries(name, rules) VALUES (%s, %s::jsonb) "
        "RETURNING id, name, version, rules, created_at, updated_at",
        (name, json.dumps(rules)),
    )
    return {"query": _fundamental_query_row(row)}


@app.put("/api/fundamental-queries/{query_id}")
def update_fundamental_query(query_id: int, request: FundamentalQueryRequest):
    rules = _canonical_fundamental_rules(request.rules)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Query name cannot be blank")
    row = get_db().execute_returning(
        "UPDATE fundamental_queries SET name = %s, rules = %s::jsonb, "
        "version = version + 1, updated_at = NOW() "
        "WHERE id = %s AND is_active = TRUE "
        "RETURNING id, name, version, rules, created_at, updated_at",
        (name, json.dumps(rules), query_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Fundamental query not found")
    return {"query": _fundamental_query_row(row)}


@app.delete("/api/fundamental-queries/{query_id}")
def delete_fundamental_query(query_id: int):
    # Soft deletion keeps the snapshot reference in completed research runs.
    affected = get_db().execute(
        "UPDATE fundamental_queries SET is_active = FALSE, updated_at = NOW() "
        "WHERE id = %s AND is_active = TRUE",
        (query_id,),
    )
    if affected == 0:
        raise HTTPException(status_code=404, detail="Fundamental query not found")
    return {"deleted": True, "query_id": query_id}


@app.post("/api/fundamental-queries/preview")
def preview_fundamental_query(request: FundamentalQueryPreviewRequest):
    rules = _canonical_fundamental_rules(request.rules)
    as_of = request.as_of or datetime.now(IST).date()
    if as_of > datetime.now(IST).date():
        raise HTTPException(status_code=422, detail="Preview date cannot be in the future")
    return evaluate_universe(get_db(), rules, as_of)


def _fundamental_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return _serialize_row(dict(job))


def _fundamental_job_update(job_id: str, **changes: Any) -> None:
    with _fundamental_backtest_lock:
        job = _fundamental_backtest_jobs.get(job_id)
        if job is not None:
            job.update(changes)


def _load_fundamental_setup_candles(
    db: Database,
    stock_id: int,
    start: date,
    end: date,
    *,
    quality_counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    # Cover the longest setup's 252-session lookback (plus weekends and market
    # holidays). Earlier 5-minute bars warm indicators only; entries stay in range.
    warmup_start = datetime.combine(start - timedelta(days=400), datetime.min.time(), tzinfo=IST)
    end_exclusive = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    rows = db.fetch_all(
        "SELECT timestamp, open, high, low, close, volume FROM candles_5m "
        "WHERE stock_id = %s AND timestamp >= %s AND timestamp < %s "
        "AND (timestamp AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15' "
        "AND (timestamp AT TIME ZONE 'Asia/Kolkata')::time <= TIME '15:30' "
        "ORDER BY timestamp",
        (stock_id, warmup_start, end_exclusive),
    )
    rows = [row for row in rows if is_regular_session_candle(row["timestamp"])]
    valid_rows = [
        row for row in rows
        if is_valid_ohlcv(row["open"], row["high"], row["low"], row["close"], row["volume"])
    ]
    invalid_count = len(rows) - len(valid_rows)
    if invalid_count:
        logger.warning(
            "Skipped {} malformed 5-minute candles for stock_id={} during fundamental backtest loading",
            invalid_count,
            stock_id,
        )
        if quality_counts is not None:
            quality_counts[Timeframe.FIVE_MINUTE.value] = (
                quality_counts.get(Timeframe.FIVE_MINUTE.value, 0) + invalid_count
            )

    # Keep valid 5-minute bars, resolving duplicate timestamps from
    # overlapping historical and live feeds in favor of the higher-volume row.
    by_timestamp: dict[datetime, dict[str, Any]] = {}
    for row in valid_rows:
        timestamp = row["timestamp"].astimezone(IST)
        previous = by_timestamp.get(timestamp)
        if previous is None or (row["volume"], row["timestamp"]) > (previous["volume"], previous["timestamp"]):
            by_timestamp[timestamp] = row
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def _run_fundamental_filter_setups(
    *,
    symbol: str,
    setup_ids: list[str],
    start_date: date,
    end_date: date,
    initial_capital: float,
    charge_config: DeliveryChargeConfig,
    quality_counts: dict[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run Watchlist Analysis presets with their original multi-timeframe rules."""
    presets = [get_filter_preset(setup_id) for setup_id in setup_ids]
    request = FilterBacktestRequest(
        symbol=symbol,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        initial_capital=initial_capital,
    )
    aggregate_requirements: dict[str, dict[str, Any]] = {}
    for preset in presets:
        _, requirements, errors = _parse_filter_expression(
            FilterRequirementsRequest(
                mode=EvaluationMode.BACKTEST,
                expression=preset.expression.model_dump(mode="json"),
            )
        )
        if errors:
            raise RuntimeError(f"Invalid Watchlist Analysis preset {preset.preset_id}: {'; '.join(errors)}")
        for timeframe, current in requirements.items():
            existing = aggregate_requirements.setdefault(
                timeframe,
                {
                    "minimum_candles": 0,
                    "warmup": 0,
                    "required_fields": [],
                    "requires_live": False,
                },
            )
            existing["minimum_candles"] = max(existing["minimum_candles"], current["minimum_candles"])
            existing["warmup"] = max(existing["warmup"], current["warmup"])
            existing["required_fields"] = sorted(set(existing["required_fields"]) | set(current["required_fields"]))
            existing["requires_live"] = existing["requires_live"] or current["requires_live"]
        aggregate_requirements.setdefault(preset.timeframe.value, {})

    shared_inputs = _load_filter_backtest_inputs(
        request,
        aggregate_requirements,
        quality_counts=quality_counts,
    )
    candles_by_timeframe = shared_inputs[4]
    atomic_cache = build_atomic_backtest_cache(
        (preset.expression for preset in presets),
        candles_by_timeframe,
        _FILTER_CATALOG,
    )
    evaluator = FilterEvaluator(_FILTER_CATALOG, atomic_cache=atomic_cache)
    results: dict[str, dict[str, Any]] = {}
    for preset in presets:
        profile = ProfileBacktester(
            symbol=symbol,
            candles_by_timeframe=candles_by_timeframe,
            expression=preset.expression,
            config=ProfileBacktestConfig(
                initial_capital=initial_capital,
                position_size_pct=100,
                charge_config=charge_config,
                # Match Watchlist Analysis request defaults and each preset's
                # original stop, target, and 5-minute holding-period policy.
                stop_loss_pct=preset.default_stop_loss_pct,
                take_profit_pct=preset.default_take_profit_pct,
                max_hold_bars=preset.max_hold_bars,
            ),
            start_date=shared_inputs[2],
            end_date=shared_inputs[3],
            execution_timeframe=preset.timeframe,
            evaluator=evaluator,
        ).run()
        primary = candles_by_timeframe[preset.timeframe]
        entry_indices = {candle.timestamp: index for index, candle in enumerate(primary)}
        trades: list[dict[str, Any]] = []
        for trade in profile["trades"]:
            entry_timestamp = datetime.fromisoformat(trade["entry_date"])
            entry_index = entry_indices.get(entry_timestamp)
            signal_timestamp = (
                primary[entry_index - 1].timestamp.isoformat()
                if entry_index is not None and entry_index > 0
                else trade["entry_date"]
            )
            trades.append({
                "setup_id": preset.preset_id,
                "signal_date": signal_timestamp,
                "entry_date": trade["entry_date"],
                "entry_price": trade["entry_price"],
                "exit_date": trade["exit_date"],
                "exit_price": trade["exit_price"],
                "return_pct": trade["pnl_pct"],
                "pnl": trade["pnl"],
                "exit_reason": trade["exit_reason"],
                "poc": trade.get("poc"),
                "swing_low": trade.get("swing_low"),
                "swing_high": trade.get("swing_high"),
                "value_area_low": trade.get("value_area_low"),
                "value_area_high": trade.get("value_area_high"),
                "stop_price": round(trade["stop_price"], 4) if trade.get("stop_price") is not None else round(trade["entry_price"] * (1 - preset.default_stop_loss_pct / 100), 4),
                "target_price": round(trade["target_price"], 4) if trade.get("target_price") is not None else round(trade["entry_price"] * (1 + preset.default_take_profit_pct / 100), 4),
                "target_r": trade.get("target_r"),
                "max_hold_bars": preset.max_hold_bars,
                "stop_buffer_pct": trade.get("stop_buffer_pct", 0.0),
                "total_charges": trade["total_charges"],
            })
        results[preset.preset_id] = {
            "metrics": {
                "total_return_pct": profile["total_return_pct"],
                "max_drawdown_pct": profile["max_drawdown_pct"],
                "trades": profile["total_trades"],
                "final_capital": profile["final_capital"],
            },
            "trades": trades,
        }
    return results


def _run_fundamental_setup_stock(
    stock: dict[str, Any],
    start_date: date,
    end_date: date,
    initial_capital: float,
    setup_ids: tuple[str, ...],
    target_r: float,
    max_hold_bars: int,
    stop_buffer_pct: float,
    charge_config: DeliveryChargeConfig,
) -> dict[str, Any]:
    """Load one stock's data and run its selected setups in an isolated worker."""
    standalone_ids = [setup_id for setup_id in setup_ids if setup_id not in FILTER_BACKED_SETUP_IDS]
    filter_ids = [setup_id for setup_id in setup_ids if setup_id in FILTER_BACKED_SETUP_IDS]
    failures: dict[str, str] = {}
    metrics_by_setup: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    standalone_quality_counts: dict[str, int] = {}
    filter_quality_counts: dict[str, int] = {}

    if standalone_ids:
        try:
            candles = _load_fundamental_setup_candles(
                get_db(), int(stock["stock_id"]), start_date, end_date,
                quality_counts=standalone_quality_counts,
            )
            if len(candles) < 2:
                raise RuntimeError("No five-minute candles are available in the requested period.")
            candle_series = prepare_setup_candles(candles)
        except Exception as exc:
            for setup_id in standalone_ids:
                failures[setup_id] = str(exc)
            logger.warning("Fundamental setup candle load skipped {}: {}", stock["symbol"], exc)
        else:
            # Every selected standalone pattern shares one validated candle
            # series, but runs independently so its bankroll and exits match
            # the serial implementation exactly.
            for setup_id in standalone_ids:
                try:
                    outcome = run_setup(
                        candles=candles,
                        candle_series=candle_series,
                        setup_id=setup_id,
                        start_date=start_date,
                        end_date=end_date,
                        initial_capital=initial_capital,
                        charge_config=charge_config,
                        target_r=target_r,
                        max_hold_bars=max_hold_bars,
                        stop_buffer_pct=stop_buffer_pct,
                    )
                    metrics_by_setup[setup_id] = outcome["metrics"]
                    trades.extend({
                        **trade,
                        "symbol": stock["symbol"],
                        "stock_name": stock.get("name") or stock["symbol"],
                        "setup_id": setup_id,
                        "setup_name": SETUPS[setup_id]["name"],
                    } for trade in outcome["trades"])
                except Exception as exc:
                    failures[setup_id] = str(exc)
                    logger.warning(
                        "Fundamental setup test skipped {} / {}: {}",
                        stock["symbol"], setup_id, exc,
                    )

    if filter_ids:
        try:
            filter_outcomes = _run_fundamental_filter_setups(
                symbol=stock["symbol"],
                setup_ids=filter_ids,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                charge_config=charge_config,
                quality_counts=filter_quality_counts,
            )
            for setup_id in filter_ids:
                outcome = filter_outcomes[setup_id]
                metrics_by_setup[setup_id] = outcome["metrics"]
                trades.extend({
                    **trade,
                    "symbol": stock["symbol"],
                    "stock_name": stock.get("name") or stock["symbol"],
                    "setup_id": setup_id,
                    "setup_name": SETUPS[setup_id]["name"],
                } for trade in outcome["trades"])
        except Exception as exc:
            for setup_id in filter_ids:
                failures[setup_id] = str(exc)
            logger.warning("Fundamental filter presets skipped {}: {}", stock["symbol"], exc)

    quality_by_timeframe = {
        timeframe: max(
            standalone_quality_counts.get(timeframe, 0),
            filter_quality_counts.get(timeframe, 0),
        )
        for timeframe in standalone_quality_counts.keys() | filter_quality_counts.keys()
    }
    return {
        "metrics_by_setup": metrics_by_setup,
        "trades": trades,
        "failures": failures,
        "invalid_candle_count": sum(quality_by_timeframe.values()),
    }


def _run_fundamental_query_backtest_job(
    job_id: str, request: FundamentalQueryBacktestRequest, query: dict[str, Any], setup_ids: list[str]
) -> None:
    _fundamental_job_update(job_id, status="running", started_at=datetime.now(timezone.utc))
    try:
        db = get_db()
        universe_as_of = request.universe_as_of or datetime.now(IST).date()
        limitations: set[str] = set()
        if request.selected_stock_ids is None:
            # Older API clients may omit the preview result. Select the saved
            # query's universe once, then keep it fixed for the full test.
            preview = evaluate_universe(
                db, query["rules"], universe_as_of, include_missing_stocks=False,
            )
            stocks = preview["stocks"]
            limitations.update(preview.get("limitations", []))
            selection_source = "saved_query"
        else:
            selected_ids = list(dict.fromkeys(request.selected_stock_ids))
            stocks = db.fetch_all(
                f"SELECT id AS stock_id, symbol, name FROM stocks "
                f"WHERE id = ANY(%s) AND {equity_universe_sql('stocks')} "
                "ORDER BY symbol",
                (selected_ids,),
            ) if selected_ids else []
            if {int(stock["stock_id"]) for stock in stocks} != set(selected_ids):
                raise RuntimeError("Could not resolve the full query-preview stock list; preview the query again.")
            selection_source = "query_preview"
        stocks = sorted(stocks, key=lambda item: item["symbol"])
        universe_selection = {
            "as_of": universe_as_of.isoformat(),
            "stock_count": len(stocks),
            "symbols": [stock["symbol"] for stock in stocks],
            "source": selection_source,
        }
        _fundamental_job_update(
            job_id,
            universe_selection=universe_selection,
            progress={"completed": 0, "total": len(stocks)},
        )

        # Each stock receives the same independent test bankroll. Rankings are
        # mean per-stock returns, not a claim that these overlapping trades
        # could all be placed in a single cash-constrained account.
        results_by_setup: dict[str, list[dict[str, Any]]] = {setup_id: [] for setup_id in setup_ids}
        all_trades: list[dict[str, Any]] = []
        failed_setup_messages: dict[str, list[str]] = {}
        failed_setup_runs = 0
        successful_setup_runs = 0
        invalid_candle_count = 0
        invalid_candle_stock_count = 0
        charge_config = config_from_store(store("config"))

        def record_setup_failure(symbol: str, setup_id: str, error: Exception | str) -> None:
            nonlocal failed_setup_runs
            failed_setup_runs += 1
            failed_setup_messages.setdefault(symbol, []).append(f"{SETUPS[setup_id]['name']}: {error}")

        _fundamental_job_update(job_id, progress={"completed": 0, "total": len(stocks)})
        stock_results: dict[int, dict[str, Any]] = {}
        if stocks:
            executor = _get_fundamental_backtest_executor()
            # Feed at most one worker batch at a time. A query can select
            # thousands of stocks, so queuing every future together needlessly
            # retains thousands of Future objects while the bounded pool runs.
            completed = 0
            for batch_start in range(0, len(stocks), _MAX_FUNDAMENTAL_BACKTEST_WORKERS):
                batch = stocks[batch_start:batch_start + _MAX_FUNDAMENTAL_BACKTEST_WORKERS]
                futures = {
                    executor.submit(
                        _run_fundamental_setup_stock,
                        stock,
                        request.start_date,
                        request.end_date,
                        FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK,
                        tuple(setup_ids),
                        request.target_r,
                        request.max_hold_bars,
                        request.stop_buffer_pct,
                        charge_config,
                    ): stock
                    for stock in batch
                }
                for future in as_completed(futures):
                    stock = futures[future]
                    try:
                        stock_results[int(stock["stock_id"])] = future.result()
                    except Exception as exc:
                        stock_results[int(stock["stock_id"])] = {
                            "metrics_by_setup": {},
                            "trades": [],
                            "failures": {setup_id: str(exc) for setup_id in setup_ids},
                        }
                        logger.warning("Fundamental setup worker failed {}: {}", stock["symbol"], exc)
                    completed += 1
                    _fundamental_job_update(job_id, progress={"completed": completed, "total": len(stocks)})

        # Merge in symbol order so floating-point aggregates and returned
        # trades remain deterministic regardless of process completion order.
        for stock in stocks:
            outcome = stock_results.get(int(stock["stock_id"]), {})
            stock_invalid_candles = int(outcome.get("invalid_candle_count", 0))
            if stock_invalid_candles:
                invalid_candle_count += stock_invalid_candles
                invalid_candle_stock_count += 1
            for setup_id in setup_ids:
                if setup_id in outcome.get("failures", {}):
                    record_setup_failure(stock["symbol"], setup_id, outcome["failures"][setup_id])
                elif setup_id in outcome.get("metrics_by_setup", {}):
                    successful_setup_runs += 1
                    results_by_setup[setup_id].append(outcome["metrics_by_setup"][setup_id])
            all_trades.extend(outcome.get("trades", []))

        failed_stocks = [
            {"symbol": symbol, "error": "; ".join(messages)}
            for symbol, messages in sorted(failed_setup_messages.items())
        ]

        leaderboard: list[dict[str, Any]] = []
        if stocks:
            for setup_id in setup_ids:
                metrics = results_by_setup[setup_id]
                trades = [trade for trade in all_trades if trade["setup_id"] == setup_id]
                count = len(metrics)
                trade_count = len(trades)
                portfolio_initial_capital = len(stocks) * FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK
                portfolio_final_capital = (
                    sum(float(item.get("final_capital", FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK)) for item in metrics)
                    + (len(stocks) - count) * FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK
                )
                portfolio_period_return_pct = (
                    round((portfolio_final_capital / portfolio_initial_capital - 1) * 100, 4)
                    if count and portfolio_initial_capital > 0
                    else None
                )
                leaderboard.append({
                    "setup_id": setup_id,
                    "setup_name": SETUPS[setup_id]["name"],
                    "stock_count": count,
                    "trades": trade_count,
                    "win_rate_pct": 100 * sum(trade["return_pct"] > 0 for trade in trades) / trade_count if trade_count else 0.0,
                    "average_return_pct": sum(trade["return_pct"] for trade in trades) / trade_count if trade_count else 0.0,
                    "total_return_pct": sum(item["total_return_pct"] for item in metrics) / count if count else 0.0,
                    "portfolio_initial_capital": round(portfolio_initial_capital, 2),
                    "portfolio_final_capital": round(portfolio_final_capital, 2),
                    "portfolio_period_return_pct": portfolio_period_return_pct,
                    "max_drawdown_pct": sum(item["max_drawdown_pct"] for item in metrics) / count if count else 0.0,
                    "return_basis": "Mean independent stock return",
                    "drawdown_basis": "Mean independent stock drawdown",
                })
        leaderboard.sort(key=lambda item: item["total_return_pct"], reverse=True)
        all_trades.sort(key=lambda item: (item.get("entry_date") or "", item["symbol"]), reverse=True)
        if not stocks:
            limitations.add(
                f"The query selected no stocks as of {universe_as_of.isoformat()}, so no technical setups were run."
            )
        if failed_setup_runs:
            limitations.add(f"{failed_setup_runs} stock/setup runs could not be tested because required price data was missing or simulation failed; see failed stock details.")
        if invalid_candle_count:
            limitations.add(
                f"Skipped {invalid_candle_count} malformed OHLCV candles across {invalid_candle_stock_count} stocks; "
                "their prices or volume were invalid and were excluded from signal and volume calculations."
            )
            limitations.add("Portfolio period return keeps the ₹1 lakh allocation for untested stocks as idle cash.")
        if universe_as_of > request.start_date:
            limitations.add(
                "The query-selected stock list was fixed after the backtest start date; applying that later selection to earlier prices is retrospective and may introduce selection bias."
            )
        limitations.add("The stock master reflects currently listed NSE equities; delisted-company coverage is incomplete, so historical comparisons can have survivorship bias.")
        if any(setup_id not in FILTER_BACKED_SETUP_IDS for setup_id in setup_ids):
            limitations.add("The standalone technical setups use five-minute candles for signals, entries, stops, targets, and exits. The longest rolling lookback spans 252 trading sessions.")
            limitations.add("Five-minute OHLCV improves the volume-by-price estimate versus daily bars but does not contain exact traded-price volume; POC still spreads each candle's volume across its high-low range.")
            if "swing_volume_profile_pullback" in setup_ids:
                limitations.add("Bullish Swing Volume-Profile Pullback confirms each pivot only after five later candles, requires two higher highs and two higher lows within its 750-candle lookback, and uses a 70% estimated value area for its entry zone.")
            limitations.add(
                f"The candle synchronizer requests up to {BACKFILL_DAYS[CandleInterval.FIVE_MINUTE]} calendar days of five-minute history. Provider limits or missing backfills can reduce a stock's actual coverage; older periods are not filled from daily candles."
            )
            limitations.add(
                "The 52-week breakout needs about 252 trading sessions of five-minute history before its first eligible signal, so early dates may be used only as warm-up."
            )
        if any(setup_id in FILTER_BACKED_SETUP_IDS for setup_id in setup_ids):
            limitations.add("Strong trend continuation and Liquid swing momentum use 5-minute execution bars aligned with completed daily context, as in Watchlist Analysis.")
        limitations.add("Configured delivery charges apply to every setup. Standalone technical setups include 0.05% adverse slippage; Watchlist Analysis presets retain their 0% slippage default.")
        limitations.add("Portfolio period return aggregates separate ₹1 lakh-per-stock wallets; it does not simulate one shared cash balance across stocks.")
        completed_at = datetime.now(timezone.utc)
        result = {
            "job_id": job_id,
            "status": (
                "failed" if stocks and successful_setup_runs == 0
                else "partial" if failed_setup_runs
                else "no_selected_stocks" if not stocks
                else "completed"
            ),
            "query": query,
            "settings": {
                **request.model_dump(mode="json"),
                "capital_per_stock": FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK,
                "candle_interval": FUNDAMENTAL_SETUP_CANDLE_INTERVAL,
            },
            "created_at": _fundamental_backtest_jobs[job_id]["created_at"],
            "started_at": _fundamental_backtest_jobs[job_id]["started_at"],
            "completed_at": completed_at,
            "progress": {"completed": len(stocks), "total": len(stocks)},
            "universe_selection": universe_selection,
            "qualification_timeline": [],
            "leaderboard": leaderboard,
            "trades": all_trades,
            "failed_stocks": failed_stocks,
            "limitations": sorted(limitations),
            "error": "All selected stock/setup runs failed simulation." if stocks and successful_setup_runs == 0 else None,
        }
        db.execute(
            "UPDATE fundamental_query_backtest_runs SET status = %s, "
            "result_json = %s::jsonb, completed_at = %s WHERE id = %s::uuid",
            (result["status"], json.dumps(_fundamental_job_snapshot(result)), completed_at, job_id),
        )
        _fundamental_job_update(job_id, **{key: value for key, value in result.items() if key != "job_id"})
    except Exception as exc:
        logger.exception("Fundamental query backtest failed")
        completed_at = datetime.now(timezone.utc)
        _fundamental_job_update(job_id, status="failed", error=str(exc), completed_at=completed_at)
        try:
            get_db().execute(
                "UPDATE fundamental_query_backtest_runs SET status = 'failed', error = %s, "
                "completed_at = %s WHERE id = %s::uuid",
                (str(exc), completed_at, job_id),
            )
        except Exception:
            logger.exception("Could not persist failed fundamental backtest")


@app.post("/api/fundamental-query-backtests")
def start_fundamental_query_backtest(request: FundamentalQueryBacktestRequest):
    if request.end_date < request.start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    today = datetime.now(IST).date()
    if request.end_date > today:
        raise HTTPException(status_code=422, detail="Backtest end date cannot be in the future")
    if request.universe_as_of and request.universe_as_of > today:
        raise HTTPException(status_code=422, detail="Universe selection date cannot be in the future")
    query = _saved_fundamental_query(request.query_id)
    setup_ids = list(dict.fromkeys(request.setup_ids or list(SETUPS)))
    if not setup_ids or any(setup_id not in SETUPS for setup_id in setup_ids):
        raise HTTPException(status_code=422, detail="Unknown technical setup selected")
    if request.selected_stock_ids is not None:
        selected_ids = list(dict.fromkeys(request.selected_stock_ids))
        if selected_ids:
            found = get_db().fetch_all(
                f"SELECT id FROM stocks WHERE id = ANY(%s) AND {equity_universe_sql('stocks')}",
                (selected_ids,),
            )
            if {int(row["id"]) for row in found} != set(selected_ids):
                raise HTTPException(
                    status_code=422,
                    detail="The selected stock list is stale. Preview the query again before running the backtest.",
                )
        request.selected_stock_ids = selected_ids
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "status": "queued",
        "query": query,
        "settings": {
            **request.model_dump(mode="json"),
            "capital_per_stock": FUNDAMENTAL_SETUP_CAPITAL_PER_STOCK,
            "candle_interval": FUNDAMENTAL_SETUP_CANDLE_INTERVAL,
        },
        "created_at": datetime.now(timezone.utc),
        "started_at": None,
        "completed_at": None,
        "progress": {"completed": 0, "total": 0},
        "qualification_timeline": [],
        "leaderboard": [],
        "trades": [],
        "failed_stocks": [],
        "limitations": [],
        "error": None,
    }
    get_db().execute(
        "INSERT INTO fundamental_query_backtest_runs"
        "(id, query_id, query_snapshot, request_json, status) "
        "VALUES (%s::uuid, %s, %s::jsonb, %s::jsonb, 'queued')",
        (job_id, request.query_id, json.dumps(query), json.dumps(job["settings"])),
    )
    with _fundamental_backtest_lock:
        while len(_fundamental_backtest_jobs) >= _MAX_FUNDAMENTAL_BACKTEST_JOBS:
            removable = [key for key, value in _fundamental_backtest_jobs.items() if value["status"] not in {"queued", "running"}]
            if not removable:
                break
            oldest = min(removable, key=lambda key: _fundamental_backtest_jobs[key]["created_at"])
            del _fundamental_backtest_jobs[oldest]
        _fundamental_backtest_jobs[job_id] = job
    Thread(
        target=_run_fundamental_query_backtest_job,
        args=(job_id, request, query, setup_ids),
        name=f"fundamental-query-backtest-{job_id[:8]}",
        daemon=True,
    ).start()
    return _fundamental_job_snapshot(job)


@app.get("/api/fundamental-query-backtests/{job_id}")
def get_fundamental_query_backtest(job_id: str):
    with _fundamental_backtest_lock:
        job = _fundamental_backtest_jobs.get(job_id)
        if job is not None:
            return _fundamental_job_snapshot(job)
    try:
        uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Fundamental backtest not found") from exc
    row = get_db().fetch_one(
        "SELECT query_snapshot, request_json, status, result_json, error, created_at, completed_at "
        "FROM fundamental_query_backtest_runs WHERE id = %s::uuid",
        (job_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Fundamental backtest not found")
    if row["result_json"]:
        return row["result_json"]
    interrupted = row["status"] in {"queued", "running"}
    return {
        "job_id": job_id,
        "status": "failed" if interrupted else row["status"],
        "query": row["query_snapshot"],
        "settings": row["request_json"],
        "created_at": row["created_at"].isoformat(),
        "started_at": None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "progress": {"completed": 0, "total": 0},
        "qualification_timeline": [],
        "leaderboard": [],
        "trades": [],
        "failed_stocks": [],
        "limitations": [],
        "error": row["error"] or ("This run was interrupted by a server restart." if interrupted else None),
    }
