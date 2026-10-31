"""
Trend-Investorr: Main Entry Point

Boots the entire trading system and orchestrates all modules:
    1. Configuration loading
    2. Security vault initialization
    3. Database connection + schema migration
    4. SmartAPI session login
    5. Instrument master loading
    6. Historical volume preloading
    7. Engine initialization (scanner, classifier, filters, signal generator)
    8. Analyzer initialization (static/gemini/composite)
    9. Trading layer initialization (risk, circuit breaker, paper trader, order manager)
    10. Scheduler registration + start
    11. WebSocket streaming (during market hours)

Usage:
    python main.py           # Start full system
    python main.py --dry-run # Boot without trading (diagnostics only)

Lifecycle:
    Pre-market (08:30)  → Login, reload instruments, refresh config
    Scanner warmup (09:00) → Load historical volumes, subscribe WebSocket
    Market open (09:15)  → Start live tick scanning
    Live trading (09:15-15:30) → Tick → Spike → Signal → Analyze → Risk → Execute
    Square-off (15:15)   → Close all intraday positions
    Market close (15:30)  → Stop WebSocket, stop scanning
    Post-market (16:00)  → Bhavcopy download, volume recalculation
    Summary (17:00)       → Daily P&L notification
    Midnight (00:00)      → Session re-login, daily reset
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

# ── Logging Configuration ────────────────────────────────
# Remove default logger, add structured format
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>:<cyan>{function}</cyan> — <level>{message}</level>",
    level="INFO",
)
logger.add(
    "logs/trend_investorr_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    compression="gz",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {module}:{function}:{line} — {message}",
)


# Fallback universe size when no watchlist tokens are available
INSTRUMENT_FALLBACK_LIMIT = 500


def prepare_live_subscription(
    watchlist_manager,
    instruments,
    stock_store,
    candle_aggregator,
    fallback_limit: int = INSTRUMENT_FALLBACK_LIMIT,
) -> list[str]:
    """
    Select the tokens to subscribe for the live session and refresh the
    CandleAggregator's token → stock_id mapping for them.

    Selection: tokens from active watchlists; if none, the first
    ``fallback_limit`` NSE instruments. The mapping is resolved through
    StockStore in one batch so live 5-minute candles can be saved with
    their stock_id. Called from scanner_warmup() on every warm-up.

    Returns:
        The selected tokens (in subscription order).
    """
    tokens: list[str] = []
    if watchlist_manager:
        tokens = list(watchlist_manager.get_subscribe_tokens() or [])

    if not tokens:
        tokens = [s.token for s in instruments.get_all_by_exchange()][:fallback_limit]
        logger.info(
            f"Scanner warm-up: no watchlist tokens found — using instruments "
            f"fallback ({len(tokens)} tokens)"
        )
    else:
        logger.info(f"Scanner warm-up: subscribing {len(tokens)} tokens from active watchlists")

    if candle_aggregator is not None and stock_store is not None:
        # Candles left over from a previous run (spill file) may belong to
        # tokens that are no longer subscribed; they still need a stock_id.
        lookup_tokens = list(tokens)
        try:
            candle_aggregator.recover_pending()
            extra = sorted(candle_aggregator.pending_tokens - set(tokens))
            if extra:
                logger.info(
                    f"Scanner warm-up: resolving stock_id for {len(extra)} token(s) with "
                    f"pending candles that are not subscribed today"
                )
                lookup_tokens += extra
        except Exception as e:
            logger.error(f"Scanner warm-up: could not inspect pending candles: {e}")

        try:
            mapping = stock_store.get_ids_by_tokens(lookup_tokens, exchange="NSE")
        except Exception as e:
            # Keep the aggregator's last valid mapping rather than wiping it;
            # a later successful warm-up will replace it.
            logger.error(
                f"Scanner warm-up: token → stock_id lookup failed: {e} — "
                f"keeping previous candle mapping"
            )
        else:
            candle_aggregator.set_token_mapping(mapping)
            unmapped = len(tokens) - sum(1 for t in tokens if t in mapping)
            if unmapped:
                logger.warning(
                    f"Scanner warm-up: {unmapped} subscribed tokens have no active NSE "
                    f"stocks row — their live candles will be held until a mapping is available"
                )
            if candle_aggregator.pending_candles:
                written = candle_aggregator.flush_pending()
                logger.info(
                    f"Scanner warm-up: wrote {written} previously pending candle(s), "
                    f"{candle_aggregator.pending_candles} still pending"
                )

    return tokens


def shutdown_live_components(candle_aggregator, tick_writer, notifier) -> int:
    """
    Stop the live persistence components on process shutdown (signal
    handler or market close), flushing what can be written and spilling
    the rest so it survives a restart.

    Returns:
        Number of candles that could not be written (0 = all saved).
    """
    if tick_writer is not None:
        try:
            tick_writer.stop()
        except Exception as e:
            logger.error(f"TickWriter stop failed: {e}")

    if candle_aggregator is None:
        return 0

    try:
        pending = candle_aggregator.stop()
        stats = candle_aggregator.stats
    except Exception as e:
        logger.error(f"CandleAggregator stop failed: {e}")
        return -1
    logger.info(f"CandleAggregator final stats: {stats}")
    alert = candle_close_alert(pending, stats)
    if alert and notifier is not None:
        try:
            notifier.system_alert("Candle Aggregator", alert)
        except Exception as e:
            logger.error(f"Candle alert failed: {e}")
    return pending


def candle_close_alert(pending: int, stats: dict) -> str | None:
    """
    Operator-facing message for candles still unsaved after the market-close
    flush, built from CandleAggregator.stop() and .stats.

    Returns None when nothing is pending.
    """
    blocked = [str(p) for p in stats.get("blocked_spill_files") or []]
    blocked_note = ""
    if blocked:
        blocked_note = (
            f" Spill file(s) {', '.join(blocked)} could not be loaded at start and were "
            f"left untouched — the candles inside are NOT saved; fix or move the file(s) "
            f"and restart."
        )

    if pending <= 0:
        return blocked_note.strip() or None

    unmapped = int(stats.get("pending_unmapped", 0))
    failed = max(pending - unmapped, 0)
    reason = f"{unmapped} awaiting stock_id, {failed} after failed DB write"
    last_error = stats.get("last_flush_error")
    if last_error:
        reason += f"; last error: {last_error}"

    if int(stats.get("spilled_candles", 0)) == pending:
        return (
            f"{pending} five-minute candle(s) unsaved at close ({reason}); "
            f"saved to {stats.get('spill_path')} and will be retried on next start."
            f"{blocked_note}"
        )
    where = stats.get("spill_path") or "no spill_path configured"
    return (
        f"{pending} five-minute candle(s) unsaved at close ({reason}); "
        f"the spill file could not be written ({where}) — "
        f"they will be LOST when the process exits.{blocked_note}"
    )


def main():
    """Boot and run the Trend-Investorr trading system."""

    logger.info("=" * 60)
    logger.info("  TREND-INVESTORR v1.0 — Volume-First Trading Bot")
    logger.info("=" * 60)

    # ── Step 1: Configuration ────────────────────────────
    from config.settings import Settings
    from core.constants import IST, MARKET_OPEN

    # When this process came up. A boot after today's market open means a
    # mid-session restart: anything the previous process held is unknown.
    boot_time = datetime.now(IST)

    try:
        settings = Settings()
        logger.info(f"Configuration loaded: DB={settings.db_name}@{settings.db_host}")
    except Exception as e:
        logger.critical(f"Configuration failed: {e}")
        sys.exit(1)

    # ── Step 2: Security ─────────────────────────────────
    from core.security.vault import SecureVault
    from core.security.audit import AuditTrail
    from core.constants import AuditEventType

    try:
        vault = SecureVault(vault_path=settings.vault_dir, master_key=getattr(settings, "VAULT_MASTER_KEY", ""))
        audit = AuditTrail(log_dir=settings.audit_dir)
        audit.log(AuditEventType.SYSTEM_STARTUP, {"version": "1.0", "mode": settings.trading_mode})
        # Import newly configured credentials (including the Upstox token) into
        # the encrypted vault. Existing values are preserved when absent from
        # the environment.
        vault.setup_from_env(settings)
        logger.info("Security vault and audit trail initialized")
    except Exception as e:
        logger.critical(f"Security initialization failed: {e}")
        sys.exit(1)

    # ── Step 3: Database ─────────────────────────────────
    from storage.database import Database
    from storage.models import (
        AppConfigStore,
        CandleStore,
        CandleCoverageStore,
        ScannerSignalStore,
        StockStore,
        TechnicalIndicatorStore,
        WatchlistStore,
        WatchlistAlgorithmBindingStore,
        FilterProfileStore,
    )
    from core.watchlist_manager import WatchlistManager

    db = Database(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_connections=settings.db_pool_min,
        max_connections=settings.db_pool_max,
    )

    # Storage layer instances (initialized after DB connects)
    config_store = None
    candle_store = None
    coverage_store = None
    stock_store = None
    indicator_store = None
    scanner_signal_store = None
    filter_profile_store = None
    watchlist_manager = None      # Facade for watchlist + binding stores

    try:
        db.connect()
        db.run_migrations()
        config_store = AppConfigStore(db)
        config_store.reload()
        candle_store = CandleStore(db)
        coverage_store = CandleCoverageStore(db)
        stock_store = StockStore(db)
        indicator_store = TechnicalIndicatorStore(db)
        scanner_signal_store = ScannerSignalStore(db)
        filter_profile_store = FilterProfileStore(db)
        watchlist_manager = WatchlistManager(
            watchlist_store=WatchlistStore(db),
            binding_store=WatchlistAlgorithmBindingStore(db),
        )
        logger.info("Database connected and migrations applied")
    except Exception as e:
        logger.error(f"Database connection failed: {e} — running in offline mode")
        db = None
        config_store = None

    # ── Step 4: SmartAPI Session ─────────────────────────
    from data.auth.smartapi_auth import SmartAPISession

    session = SmartAPISession(vault=vault, audit=audit)
    try:
        session.login()
        logger.info("SmartAPI session established")
    except Exception as e:
        logger.warning(f"SmartAPI login failed: {e} — broker features unavailable")

    # ── Step 5: Instrument Loading ───────────────────────
    from data.fetchers.instrument_loader import InstrumentLoader

    instruments = InstrumentLoader(cache_dir=settings.data_dir / "instruments")
    try:
        instruments.load()
        logger.info(f"Instruments loaded: {len(instruments)} instruments")
        if db:
            instruments.sync_to_db(db)
    except Exception as e:
        logger.warning(f"Instrument loading failed: {e}")

    # ── Step 6: Engine Layer ─────────────────────────────
    from engine.volume_scanner import VolumeScanner
    from engine.sector_classifier import SectorClassifier
    from engine.filters import StockFilter, FilterConfig
    from engine.breakout_scanner import BreakoutScanner
    from engine.orb_scanner import ORBScanner
    from engine.momentum_scanner import MomentumScanner
    from engine.orderflow_scanner import OrderFlowScanner
    from engine.gap_scanner import GapScanner
    from engine.vwap_scanner import VWAPScanner
    from engine.relative_strength_scanner import RelativeStrengthScanner
    from engine.delivery_scanner import DeliveryScanner
    from engine.pattern_scanner import PatternScanner
    from engine.mtf_scanner import MTFScanner
    from engine.sector_rotation_scanner import SectorRotationScanner
    from engine.liquidity_sweep_scanner import LiquiditySweepScanner

    sector_classifier = SectorClassifier(
        sectors_file=Path("config/sectors.json"),
    )

    stock_filter = StockFilter(config=FilterConfig(
        min_price=config_store.get_float("min_price", 50.0) if config_store else 50.0,
        max_price=config_store.get_float("max_price", 5000.0) if config_store else 5000.0,
        min_avg_volume=config_store.get_int("min_avg_volume", 50000) if config_store else 50000,
    ))

    volume_scanner = VolumeScanner(
        spike_multiplier=config_store.get_float("spike_multiplier", 2.0) if config_store else 2.0,
    )


    # ── New Scanners ──
    breakout_scanner = BreakoutScanner()
    orb_scanner = ORBScanner(
        orb_minutes=config_store.get_int("orb_minutes", 15) if config_store else 15,
    )
    momentum_scanner = MomentumScanner()
    orderflow_scanner = OrderFlowScanner()
    gap_scanner = GapScanner()
    vwap_scanner = VWAPScanner()
    rs_scanner = RelativeStrengthScanner()
    delivery_scanner = DeliveryScanner()
    pattern_scanner = PatternScanner()
    mtf_scanner = MTFScanner()
    sector_rotation_scanner = SectorRotationScanner()
    liquidity_sweep_scanner = LiquiditySweepScanner(
        min_depth_pct=0.10,
        max_depth_pct=1.50,
        max_reclaim_seconds=120.0,
        cooldown_minutes=60,
    )

    # ── Algorithm Manager (ALL trading decisions) ──
    from strategies.algorithm_manager import AlgorithmManager
    from strategies.connors_rsi_pullback import ConnorsRSIPullback
    from strategies.scanner_confluence_algo import ScannerConfluenceAlgo

    algorithm_manager = AlgorithmManager(eval_interval_ticks=50)

    # Algorithm 1: Scanner Confluence (was StrategyEngine — enters when 2+ scanners agree)
    confluence_algo = ScannerConfluenceAlgo(
        min_confluence=config_store.get_float("min_confluence", 0.35) if config_store else 0.35,
        min_scanners=config_store.get_int("min_scanners", 2) if config_store else 2,
        max_entries_per_cycle=config_store.get_int("max_decisions_per_cycle", 5) if config_store else 5,
    )
    algorithm_manager.register(confluence_algo)

    # Algorithm 2: Connors RSI Pullback (buy oversold stocks in uptrends)
    connors_rsi = ConnorsRSIPullback(
        rsi_entry_threshold=config_store.get_float("connors_rsi_entry", 25.0) if config_store else 25.0,
        rsi_exit_threshold=config_store.get_float("connors_rsi_exit", 40.0) if config_store else 40.0,
        max_hold_candles=config_store.get_int("connors_max_hold", 10) if config_store else 10,
    )
    algorithm_manager.register(connors_rsi)

    logger.info(
        f"Engine layer initialized: 13 scanners + "
        f"AlgorithmManager ({len(algorithm_manager._algorithms)} algorithms)"
    )

    # ── Step 7: Analyzers ────────────────────────────────
    from analyzers.registry import AnalyzerRegistry

    analyzer_registry = AnalyzerRegistry(vault=vault)
    analyzer_type = config_store.get("analyzer_type", "static") if config_store else "static"
    analyzer = analyzer_registry.create(analyzer_type)
    logger.info(f"Analyzer active: {analyzer.name} (healthy={analyzer.health_check()})")

    # ── Step 8: Trading Layer ────────────────────────────
    from trading.risk_manager import RiskManager, RiskConfig
    from trading.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
    from trading.paper_trader import PaperTrader
    from trading.order_manager import OrderManager, TradingMode

    total_capital = config_store.get_float("total_capital", 1_00_00_000.0) if config_store else 1_00_00_000.0

    risk_config = RiskConfig(
        total_capital=total_capital,
        max_capital_per_trade_pct=config_store.get_float("max_capital_per_trade_pct", 5.0) if config_store else 5.0,
        max_daily_loss_pct=config_store.get_float("max_daily_loss_pct", 2.0) if config_store else 2.0,
        max_open_positions=config_store.get_int("max_open_positions", 20) if config_store else 20,
        default_stop_loss_pct=config_store.get_float("default_stop_loss_pct", 2.0) if config_store else 2.0,
        default_target_pct=config_store.get_float("default_target_pct", 5.0) if config_store else 5.0,
    )
    risk_manager = RiskManager(config=risk_config, audit=audit)

    cb_config = CircuitBreakerConfig(
        max_daily_loss_pct=config_store.get_float("circuit_breaker_daily_loss_pct", 3.0) if config_store else 3.0,
        max_consecutive_losses=config_store.get_int("circuit_breaker_consecutive_losses", 5) if config_store else 5,
    )
    circuit_breaker = CircuitBreaker(config=cb_config, audit=audit, total_capital=total_capital)

    paper_trader = PaperTrader(
        capital=total_capital,
        slippage_pct=config_store.get_float("slippage_pct", 0.1) if config_store else 0.1,
        audit=audit,
    )

    trading_mode_str = config_store.get("trading_mode", "PAPER") if config_store else "PAPER"
    trading_mode = TradingMode(trading_mode_str)

    # Durable record of live square-off exits (one file per trading day +
    # instrument + side). Shared by every process instance on this host so a
    # restart, or a second instance, never sends a second exit while the
    # broker has not listed the first one yet.
    from trading.entry_reservation_store import EntryReservationStore
    from trading.exit_intent_store import ExitIntentStore

    exit_intents = ExitIntentStore(settings.data_dir / "square_off_intents")
    # Durable record of live BUYs submitted today: an order the broker
    # accepted but has not listed yet, or whose reply was lost, must not be
    # sent again by this or a fresh process. Without it no live BUY is sent.
    entry_reservations = EntryReservationStore(settings.data_dir / "entry_reservations")

    order_manager = OrderManager(
        risk_manager=risk_manager,
        circuit_breaker=circuit_breaker,
        paper_trader=paper_trader,
        audit=audit,
        mode=trading_mode,
        live_session=session if trading_mode != TradingMode.PAPER else None,
        exit_intents=exit_intents,
        entry_reservations=entry_reservations,
    )

    logger.info(f"Trading layer initialized: mode={trading_mode.value}, capital=₹{total_capital:,.0f}")

    # ── Step 9: Notifications ────────────────────────────
    from notifications.notifier import Notifier, NotificationPriority

    notifier = Notifier(
        telegram_token=vault.get_credential("TELEGRAM_BOT_TOKEN") or "",
        telegram_chat_id=vault.get_credential("TELEGRAM_CHAT_ID") or "",
    )
    notifier.system_alert("System Started", f"Mode: {trading_mode.value} | Analyzer: {analyzer.name}")

    # ── Step 10: Data Ingestion Service ───────────────────
    from data.data_ingestion import DataIngestionService
    from data.fetchers.historical_fetcher import HistoricalFetcher

    # HistoricalFetcher is ONLY used by DataIngestionService (background pipeline).
    # It is NEVER called on the live tick path.
    historical_fetcher = HistoricalFetcher(session=session)
    ingestion = None
    if db and candle_store and stock_store:
        ingestion = DataIngestionService(
            historical_fetcher=historical_fetcher,
            candle_store=candle_store,
            stock_store=stock_store,
            coverage_store=coverage_store,
        )
        # Connect indicator store for compute + warm-up
        if indicator_store:
            ingestion.set_indicator_store(indicator_store)
        logger.info("Data ingestion service initialized")

    # Optional published filter profile. Disabled unless explicitly configured;
    # existing algorithms and their exit/risk behavior remain unchanged.
    profile_id = config_store.get_int("active_filter_profile_id", 0) if config_store else 0
    profile_enabled = config_store.get_bool("filter_profile_enabled", False) if config_store else False
    if profile_enabled and profile_id > 0 and filter_profile_store and candle_store and stock_store:
        try:
            from strategies.profile_loader import ProfileLoader
            from strategies.profile_rule_algo import ProfileRuleAlgorithm
            from engine.filter_engine.context import build_evaluation_context
            from engine.filter_engine.models import EvaluationMode, Timeframe
            from core.constants import CandleInterval
            from data.models.market_data import Candle

            loaded_profile = ProfileLoader(filter_profile_store).load(profile_id)
            interval_map = {
                Timeframe.ONE_MINUTE: CandleInterval.ONE_MINUTE,
                Timeframe.FIVE_MINUTE: CandleInterval.FIVE_MINUTE,
                Timeframe.FIFTEEN_MINUTE: CandleInterval.FIFTEEN_MINUTE,
                Timeframe.ONE_HOUR: CandleInterval.ONE_HOUR,
                Timeframe.ONE_DAY: CandleInterval.ONE_DAY,
                Timeframe.ONE_WEEK: CandleInterval.ONE_WEEK,
            }

            def profile_context_provider(observation):
                stock = stock_store.get_by_symbol(observation.symbol)
                if not stock:
                    raise ValueError(f"stock not found for profile evaluation: {observation.symbol}")
                series_by_timeframe = {}
                for timeframe_name in loaded_profile.requirements:
                    timeframe = Timeframe(timeframe_name)
                    interval = interval_map[timeframe]
                    days = 365 if timeframe is Timeframe.ONE_DAY else 90
                    rows = candle_store.get_candles(stock["id"], interval, days=days)
                    series_by_timeframe[timeframe] = [
                        Candle(
                            symbol=observation.symbol,
                            timestamp=row["timestamp"],
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=int(row["volume"]),
                            interval=interval,
                        )
                        for row in reversed(rows)
                    ]
                return build_evaluation_context(
                    symbol=observation.symbol,
                    as_of=observation.timestamp,
                    mode=EvaluationMode.SIGNAL,
                    candles=series_by_timeframe,
                    stock_metadata=stock,
                    live_snapshot=observation.details.get("live_snapshot", {}),
                )

            algorithm_manager.register(
                ProfileRuleAlgorithm(
                    profile_id=loaded_profile.profile_id,
                    profile_version=loaded_profile.version_number,
                    expression=loaded_profile.expression,
                    context_provider=profile_context_provider,
                    stop_loss_pct=float(loaded_profile.risk_config.get("stop_loss_pct", 1.0)),
                    target_pct=float(loaded_profile.risk_config.get("target_pct", 2.0)),
                )
            )
            logger.info(
                f"Published filter profile enabled: id={loaded_profile.profile_id} "
                f"version={loaded_profile.version_number} mode={trading_mode.value}"
            )
        except Exception as exc:
            logger.error(f"Filter profile disabled: could not load profile {profile_id}: {exc}")

    # ── Step 11: WebSocket ───────────────────────────────
    from data.streams.websocket_manager import WebSocketManager
    from data.streams.tick_writer import TickWriter
    from data.streams.candle_aggregator import CandleAggregator
    from core.constants import FeedMode

    ws_manager = WebSocketManager(session=session, audit=audit, feed_mode=FeedMode.QUOTE)

    # Tick Writer: batches raw ticks → live_ticks table
    tick_writer = None
    if db:
        tick_writer = TickWriter(db=db)
        logger.info("TickWriter initialized (will start with market open)")

    # Candle Aggregator: ticks → 5-minute OHLCV candles → candles_5m table
    candle_aggregator = None
    if db:
        candle_aggregator = CandleAggregator(
            db=db,
            interval_min=5,
            # Candles still unsaved at shutdown are spilled here and
            # recovered on the next start.
            spill_path=settings.data_dir / "candle_spill_5m.json",
        )
        logger.info("CandleAggregator initialized (5M candles)")

    def announce_order(signal, result, *, is_paper: bool) -> None:
        """
        Notify about a successful order result without overstating it: a
        FILLED result is a trade; a PLACED live order is only an
        acknowledged placement (no fill quantity or price is known yet).
        """
        from core.constants import OrderStatus

        side = signal.signal_type.value
        if result.status == OrderStatus.FILLED:
            notifier.trade_alert(
                symbol=signal.symbol, side=side,
                quantity=result.filled_qty, fill_price=result.avg_fill_price,
                is_paper=is_paper,
            )
        else:
            notifier.system_alert(
                f"{'PAPER' if is_paper else 'LIVE'} {side} placed — fill NOT confirmed: {signal.symbol}",
                f"Qty placed: {result.placed_qty} | broker order {result.broker_order_id or '?'} | "
                f"status {result.status.value}",
                priority=NotificationPriority.HIGH,
            )

    def announce_uncertain(signal, result, *, leg: str) -> None:
        """
        A submission whose outcome the broker never confirmed. It is NOT
        "not placed": it may be working. Said so, loudly, and kept apart
        from rejections.
        """
        notifier.risk_alert(
            f"{leg}: {signal.signal_type.value} {signal.symbol} SUBMITTED — outcome UNKNOWN",
            f"Neither placement nor rejection was confirmed by the broker: {result.message}\n"
            f"The order may be working. Its reservation/intent is kept and reconciled against the "
            f"broker's books; check the broker's order book.",
        )

    # In-memory candle cache populated during scanner_warmup (9:00 AM).
    # Keyed by token → list[Candle]. Used by the analyzer on the hot path.
    _candle_cache: dict[str, list] = {}
    # In-memory indicator cache populated during scanner_warmup (9:00 AM).
    # Keyed by token → TechnicalIndicators. Attached to signals for zero-latency analysis.
    _indicator_cache: dict[str, any] = {}
    # In-memory sector heat cache. Keyed by sector_name → SectorHeat.
    # Updated periodically during market hours for RS and SectorRotation scanners.
    _sector_heat_cache: dict[str, any] = {}

    # ── Pipeline Helpers ──

    def _get_next_trading_day(from_date=None):
        """Get the next trading day (skips weekends)."""
        from datetime import timedelta
        d = from_date or datetime.now(IST).date()
        d += timedelta(days=1)
        while d.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
            d += timedelta(days=1)
        return d

    def _scanner_name_to_source(name: str):
        """Map scanner name string to SignalSource enum."""
        from core.constants import SignalSource
        mapping = {
            "delivery": SignalSource.DELIVERY,
            "pattern": SignalSource.PATTERN,
            "mtf": SignalSource.MTF_CONFLUENCE,
        }
        return mapping.get(name, SignalSource.PATTERN)

    # ── Signal Processing Helper ──
    def _process_signal(signal, source_name: str):
        """
        Common signal processing pipeline: Analyze → Risk → Execute.
        Shared by all scanners to avoid code duplication.
        """
        try:
            # Attach pre-computed indicators from cache (zero-cost)
            cached_indicators = _indicator_cache.get(signal.token)
            if cached_indicators:
                signal.metadata["precomputed_indicators"] = cached_indicators

            # Read candles from in-memory cache (populated at 9:00 AM warm-up)
            candles = _candle_cache.get(signal.token, [])

            # Fallback: if not in cache, try DB query (rare edge case)
            if not candles and candle_store:
                from data.models.market_data import Candle
                from core.constants import CandleInterval
                stock_row = stock_store.get_by_symbol(signal.symbol) if stock_store else None
                if stock_row:
                    db_rows = candle_store.get_candles(stock_row["id"], CandleInterval.ONE_DAY, days=90)
                    candles = [
                        Candle(
                            symbol=signal.symbol,
                            timestamp=r["timestamp"],
                            open=float(r["open"]),
                            high=float(r["high"]),
                            low=float(r["low"]),
                            close=float(r["close"]),
                            volume=int(r["volume"]),
                            interval=CandleInterval.ONE_DAY,
                        )
                        for r in reversed(db_rows)  # DB returns DESC, we need ASC
                    ]
                    if candles:
                        _candle_cache[signal.token] = candles
                        logger.debug(f"Cache miss for {signal.symbol} — loaded {len(candles)} candles from DB")

            analysis = analyzer.analyze(signal, candles)

            if analysis.is_actionable:
                from core.constants import OrderStatus as _OrderStatus

                order_result = order_manager.execute_signal(signal, analysis)
                if order_result and order_result.success:
                    announce_order(signal, order_result, is_paper=(trading_mode != TradingMode.LIVE))
                elif order_result is not None and order_result.status == _OrderStatus.UNKNOWN:
                    announce_uncertain(signal, order_result, leg="LIVE")

                # DUAL mode: the result above is the paper leg. Report the
                # live leg on its own — a broker rejection must be visible,
                # and a live placement must be announced even if the paper
                # leg had nothing to sell (e.g. after a restart).
                live_leg = getattr(order_result, "live_leg", None) if order_result else None
                if live_leg is not None:
                    if live_leg.success:
                        announce_order(signal, live_leg, is_paper=False)
                    elif live_leg.status == _OrderStatus.UNKNOWN:
                        logger.error(
                            f"[{source_name}] DUAL live leg outcome UNKNOWN for {signal.symbol}: "
                            f"{live_leg.message}"
                        )
                        announce_uncertain(signal, live_leg, leg="DUAL live leg")
                    else:
                        logger.warning(
                            f"[{source_name}] DUAL live leg rejected for {signal.symbol}: "
                            f"{live_leg.message}"
                        )
                        notifier.risk_alert(
                            f"DUAL: live leg rejected — {signal.symbol}",
                            f"{signal.signal_type.value} not placed at broker: {live_leg.message}",
                        )
            else:
                logger.debug(f"[{source_name}] {signal.symbol} not actionable: {analysis.reasoning[:80]}")
        except Exception as e:
            logger.error(f"[{source_name}] Signal processing error for {signal.symbol}: {e}")

    # Register tick callback — scanners emit OBSERVATIONS, algorithms DECIDE
    _tick_count = 0  # Counter for periodic strategy evaluation
    _eval_interval = 50  # Evaluate strategy every N ticks

    def entry_to_signal_bridge(entry):
        """Convert an algorithm EntrySignal to a Signal for the execution pipeline."""
        from core.constants import SignalSource, SignalType
        from data.models.market_data import Signal, SignalStrength
        signal_type = SignalType.BUY if entry.direction == "BUY" else SignalType.SELL
        strength = SignalStrength.STRONG if entry.confidence >= 0.6 else SignalStrength.MODERATE
        primary_source = (
            entry.triggering_observations[0].source
            if entry.triggering_observations else SignalSource.MOMENTUM
        )
        return Signal(
            symbol=entry.symbol,
            token=entry.token,
            signal_type=signal_type,
            strength=strength,
            score=entry.confidence,
            price=entry.price,
            source=primary_source,
            metadata={
                "algorithm": entry.algorithm_name,
                "stop_loss": entry.stop_loss,
                "target_price": entry.target_price,
                "entry_reason": entry.reason,
                **entry.metadata,
            },
        )

    def on_tick_handler(tick):
        """
        Process each tick through all scanners and trading algorithms.
        Scanners emit observations → StrategyEngine + Algorithms evaluate → trades.
        """
        nonlocal _tick_count

        # Store raw tick to database (batched, non-blocking)
        if tick_writer:
            tick_writer.buffer(tick)

        # Aggregate into 5-minute candles
        if candle_aggregator:
            candle_aggregator.on_tick(tick)

        # ── Phase 1: Run all scanners, collect observations ──
        # All observations go to AlgorithmManager which routes to interested algorithms.

        # Scanner 1: Volume Spike
        vol_result = volume_scanner.on_tick(tick)
        if vol_result and vol_result.is_spike:
            from core.constants import SignalSource
            from data.models.market_data import Observation as _Obs
            vol_obs = _Obs(
                symbol=tick.symbol, token=tick.token,
                source=SignalSource.VOLUME_SPIKE,
                direction="bullish" if vol_result.buy_sell_ratio >= 0.8 else "bearish",
                confidence=min(vol_result.time_normalized_ratio / 5.0, 0.90),
                price=tick.ltp,
                pattern_name="volume_spike",
                details={"volume_ratio": round(vol_result.time_normalized_ratio, 2),
                         "buy_sell_ratio": round(vol_result.buy_sell_ratio, 2)},
            )
            algorithm_manager.on_observation(vol_obs)

        # Scanner 2: Breakout
        breakout_result = breakout_scanner.on_tick(tick)
        if breakout_result:
            algorithm_manager.on_observation(breakout_scanner.to_observation(breakout_result))

        # Scanner 3: ORB
        orb_result = orb_scanner.on_tick(tick)
        if orb_result:
            algorithm_manager.on_observation(orb_scanner.to_observation(orb_result))

        # Scanner 4: Momentum
        momentum_result = momentum_scanner.on_tick(tick)
        if momentum_result:
            algorithm_manager.on_observation(momentum_scanner.to_observation(momentum_result))

        # Scanners 5-9: Need VolumeTracker state
        tracker = volume_scanner.get_tracker(tick.token)
        if tracker:
            vol_ratio = vol_result.time_normalized_ratio if vol_result else 0.0

            of_result = orderflow_scanner.evaluate(tracker, tick, volume_ratio=vol_ratio)
            if of_result:
                algorithm_manager.on_observation(orderflow_scanner.to_observation(of_result))

            gap_result = gap_scanner.evaluate(tracker, tick)
            if gap_result:
                algorithm_manager.on_observation(gap_scanner.to_observation(gap_result))

            vwap_result = vwap_scanner.evaluate(tracker, tick)
            if vwap_result:
                algorithm_manager.on_observation(vwap_scanner.to_observation(vwap_result))

            # Scanner 8: Liquidity Sweep (stop hunts)
            sweep_result = liquidity_sweep_scanner.evaluate(tracker, tick, volume_ratio=vol_ratio)
            if sweep_result:
                algorithm_manager.on_observation(liquidity_sweep_scanner.to_observation(sweep_result))

            # Scanner 9: Relative Strength (stock vs sector performance)
            cached_sector = sector_classifier._stock_sector_cache.get(tick.symbol, "") if sector_classifier else ""
            cached_heat = _sector_heat_cache.get(cached_sector)
            sector_avg = cached_heat.avg_change_pct if cached_heat else 0.0
            rs_result = rs_scanner.evaluate(tick, sector_avg_change_pct=sector_avg)
            if rs_result:
                algorithm_manager.on_observation(rs_scanner.to_observation(rs_result))

        # ── Phase 2: Update active trades + check exits ──
        algorithm_manager.on_tick(tick)

        algo_exits = algorithm_manager.check_exits_only()
        for exit_sig in algo_exits:
            from core.constants import SignalSource as _SS, SignalType as _ST
            from data.models.market_data import Signal as _Signal, SignalStrength as _SSt
            exit_signal = _Signal(
                symbol=exit_sig.trade.symbol,
                token=exit_sig.trade.token,
                signal_type=_ST.SELL,
                strength=_SSt.STRONG,
                score=0.80,
                price=exit_sig.exit_price,
                source=_SS.MOMENTUM,
                metadata={
                    "algorithm": exit_sig.trade.algorithm_name,
                    "exit_reason": exit_sig.reason.value,
                    "description": exit_sig.description,
                    "entry_price": exit_sig.trade.entry_price,
                    "pnl_pct": round(exit_sig.trade.unrealized_pnl_pct, 2),
                },
            )
            _process_signal(exit_signal, f"AlgoExit[{exit_sig.trade.algorithm_name}]")
            algorithm_manager.register_exit_fill(exit_sig)

        # ── Phase 3: Periodically evaluate algorithms for entries ──
        _tick_count += 1
        if _tick_count >= _eval_interval:
            _tick_count = 0

            # Periodic: Sector Rotation scan (self-throttled to every 15 min)
            if _sector_heat_cache:
                heat_dict = {
                    name: sh.to_dict() for name, sh in _sector_heat_cache.items()
                }
                rotation_results = sector_rotation_scanner.evaluate(heat_dict)
                for rot_result in rotation_results:
                    for obs in sector_rotation_scanner.to_observations(rot_result):
                        algorithm_manager.on_observation(obs)

            # Evaluate all algorithms for entries
            algo_entries, _ = algorithm_manager.evaluate()
            for entry in algo_entries:
                signal = entry_to_signal_bridge(entry)
                _process_signal(signal, f"Algo[{entry.algorithm_name}]")

    ws_manager.on_tick(on_tick_handler)

    # ── Step 12: Scheduler ───────────────────────────────
    from scheduler.market_scheduler import MarketScheduler
    from data.fetchers.bhavcopy_fetcher import BhavcopyFetcher

    from core.session_readiness import SessionReadiness

    bhavcopy = BhavcopyFetcher(cache_dir=settings.data_dir / "bhavcopy")
    scheduler = MarketScheduler(audit=audit)
    # What has completed today. market_open() refuses to start the feed
    # unless pre-market and warm-up both succeeded (scheduled or replayed).
    readiness = SessionReadiness()

    def pre_market():
        """
        Pre-market boot: reload instruments, refresh config, purge old data.

        Required for the session (failure fails the job, and market open
        will refuse): broker authentication and the instrument master.
        """
        readiness.reset()

        try:
            session.ensure_authenticated()
        except Exception as e:
            readiness.fail("authentication", e)
            raise RuntimeError(f"authentication failed: {e}") from e

        try:
            instruments.load()
        except Exception as e:
            readiness.fail("instrument loading", e)
            raise RuntimeError(f"instrument loading failed: {e}") from e

        if db:
            try:
                instruments.sync_to_db(db)
            except Exception as e:
                logger.error(f"Pre-market: instrument DB sync failed: {e} — not required for trading")
        if config_store:
            config_store.reload()
        volume_scanner.reset_daily()
        breakout_scanner.reset_daily()
        orb_scanner.reset_daily()
        momentum_scanner.reset_daily()
        orderflow_scanner.reset_daily()
        gap_scanner.reset_daily()
        vwap_scanner.reset_daily()
        rs_scanner.reset_daily()
        sector_rotation_scanner.reset_daily()
        liquidity_sweep_scanner.reset_daily()
        algorithm_manager.reset_daily()  # Reset all trading algorithms
        risk_manager.reset_daily()
        circuit_breaker.reset_daily()
        stock_filter.reset_stats()

        # Purge old tick data (keep last 3 days) — housekeeping, not required
        if tick_writer:
            try:
                tick_writer.purge_old_ticks(days=3)
            except Exception as e:
                logger.error(f"Pre-market: tick purge failed: {e}")

        # Reset candle aggregator for new day
        if candle_aggregator:
            candle_aggregator.reset_daily()

        readiness.pre_market_ok = True
        logger.info("Pre-market boot completed")

    def scanner_warmup(require_pre_market: bool = True):
        """
        Load historical volumes from DB and subscribe WebSocket tokens.

        Flow:
            1. Build observation router from watchlist bindings (via WatchlistManager)
            2. Subscribe watchlist stocks to WebSocket
            3. Check candle_coverage and backfill any gaps
            4. Load volumes + candle cache + indicators from DB into memory

        Required for the session (failure fails the job, and market open
        will refuse): the WebSocket subscription and the warm-up data.
        Backfill is not required: if it fails the feed may still run, but
        the trading data is unverified — new entries stay blocked and the
        session is reported as degraded.

        ``require_pre_market=False`` is for the after-close catch-up, which
        only needs the caches for post-market analysis.
        """
        nonlocal _candle_cache, _indicator_cache

        if require_pre_market and not readiness.pre_market_ok:
            readiness.fail("scanner_warmup", "pre-market did not complete today")
            raise RuntimeError(
                "pre-market did not complete today — warm-up skipped (dependent step)"
            )
        readiness.warmup_ok = False
        readiness.data_verified = False

        # 1. Build observation router from watchlist ↔ algorithm bindings
        #    WatchlistManager returns a WatchlistRouter (filtered) or
        #    PassthroughRouter (fallback) depending on DB bindings.
        if watchlist_manager:
            router = watchlist_manager.load_router()
            algorithm_manager.set_router(router)

        # 2. Subscribe stocks from active watchlists to WebSocket
        #    If watchlists have stocks, use them. Otherwise fall back to instruments[:500].
        #    Also refreshes the CandleAggregator token → stock_id mapping so
        #    live 5-minute candles are saved with their stock_id.
        all_tokens = prepare_live_subscription(
            watchlist_manager=watchlist_manager,
            instruments=instruments,
            stock_store=stock_store,
            candle_aggregator=candle_aggregator,
        )

        if not all_tokens:
            readiness.fail("subscription", "no tokens to subscribe (watchlists and instruments empty)")
            raise RuntimeError("no tokens to subscribe — nothing to scan")
        try:
            ws_manager.subscribe(all_tokens)
        except Exception as e:
            readiness.fail("subscription", e)
            raise RuntimeError(f"WebSocket subscription failed: {e}") from e

        # 2. Check coverage and backfill gaps (smart — skips already-complete stocks)
        #    Not required: a failure leaves the trading data unverified.
        backfill_ok = False
        if ingestion:
            try:
                logger.info("Checking candle_coverage for gaps...")
                backfill_stats = ingestion.backfill_all_intervals()
                for iv, stats in backfill_stats.items():
                    stored = stats.get("candles_stored", 0)
                    skipped = stats.get("skipped", 0)
                    if stored > 0:
                        logger.info(f"  {iv}: {stored:,} candles filled, {skipped} skipped")
                    else:
                        logger.info(f"  {iv}: up to date ({skipped} stocks complete)")
                backfill_ok = True
            except Exception as e:
                logger.error(f"Startup backfill failed: {e} — continuing with existing data (UNVERIFIED)")
                readiness.degrade("backfill", e)

        # 3. Load historical volume baselines + candle cache from DB — required
        if ingestion is None:
            readiness.fail("warm-up data", "no data ingestion service (database offline)")
            raise RuntimeError("warm-up data cannot be loaded — database offline")
        if ingestion:
            try:
                volumes, candle_cache_data, indicator_data = ingestion.load_warmup_data(days=90)

                # Feed volume baselines into the scanner
                volume_scanner.set_historical_volumes(volumes)

                # Populate the in-memory candle cache for the analyzer
                _candle_cache = candle_cache_data

                # Populate the in-memory indicator cache for the hot path
                _indicator_cache = indicator_data

                # Wire indicator cache to all scanners that need it
                breakout_scanner.set_indicator_cache(_indicator_cache)
                orb_scanner.set_indicator_cache(_indicator_cache)
                momentum_scanner.set_indicator_cache(_indicator_cache)
                vwap_scanner.set_indicator_cache(_indicator_cache)
                rs_scanner.set_indicator_cache(_indicator_cache)
                liquidity_sweep_scanner.set_indicator_cache(_indicator_cache)

                # Wire indicator cache to trading algorithms
                connors_rsi.set_indicator_cache(_indicator_cache)

                logger.info(
                    f"Scanner warm-up complete: {len(volumes)} volume baselines, "
                    f"{len(_candle_cache)} candle caches, "
                    f"{len(_indicator_cache)} indicator caches loaded from DB"
                )
            except Exception as e:
                readiness.fail("warm-up data", e)
                raise RuntimeError(f"warm-up data loading failed: {e}") from e

        # 4. Pre-seed algorithms with overnight scanner signals
        if scanner_signal_store:
            try:
                from data.models.market_data import Observation as _WarmObs
                today = datetime.now(IST).date()
                overnight = scanner_signal_store.get_active_for_date(today)

                if overnight:
                    for sig in overnight:
                        obs = _WarmObs(
                            symbol=sig["symbol"],
                            token=sig["token"],
                            source=_scanner_name_to_source(sig["scanner_name"]),
                            direction=sig["direction"],
                            confidence=float(sig["confidence"]),
                            price=float(sig["price"]),
                            pattern_name=sig["pattern"],
                            details=sig.get("details", {}),
                        )
                        algorithm_manager.on_observation(obs)

                    # Mark as consumed so they don't reload on restart
                    scanner_signal_store.mark_consumed([s["id"] for s in overnight])

                    logger.info(
                        f"Pre-seeded {len(overnight)} overnight scanner signals "
                        f"into algorithms (delivery/pattern/MTF)"
                    )

                # Expire old signals (older than 7 days)
                from datetime import timedelta
                expired = scanner_signal_store.expire_old(today - timedelta(days=7))
                if expired:
                    logger.debug(f"Expired {expired} old scanner signals")

            except Exception as e:
                logger.error(f"Overnight signal pre-seeding failed: {e}")
                readiness.degrade("overnight signals", e)

        # 5. Readiness: warm-up done; data verified only if the backfill succeeded.
        readiness.warmup_ok = True
        readiness.data_verified = backfill_ok
        if backfill_ok:
            risk_manager.release_entry_hold("data_unverified")
        else:
            risk_manager.close_new_entries(
                "trading data unverified — candle backfill failed", key="data_unverified"
            )

    def market_open():
        """
        Start live scanning and data persistence — only on a prepared session.

        Refuses (job fails, risk alert) unless today's pre-market and warm-up
        both completed, whether they ran on schedule or were replayed. With
        unverified data (backfill failed) the feed still starts, but the
        session is reported as degraded and new entries stay blocked.
        """
        if not readiness.ready_for_open:
            msg = (
                f"Market open refused — session not ready: {readiness.blockers()}. "
                f"Live feed NOT started. Fix the cause, then re-run pre-market / warm-up / "
                f"market open (or restart)."
            )
            logger.error(msg)
            notifier.risk_alert("Market open refused — session not ready", msg)
            raise RuntimeError(msg)

        # Consumers before the producer: the tick writer and candle
        # aggregator must be running before the socket can deliver its first
        # tick (ticks would otherwise pile up with no flush thread, and the
        # aggregator's spill recovery would run after buckets were already
        # touched). If any component fails to start, everything started so
        # far is stopped again, in reverse order — a half-persisted feed is
        # never left running. Entry holds are not touched here.
        components = [
            ("tick writer", tick_writer),
            ("candle aggregator", candle_aggregator),
            ("websocket", ws_manager),  # last: only now can ticks arrive
        ]
        started: list[tuple[str, object]] = []
        for name, component in components:
            if component is None:
                continue
            try:
                component.start()
            except Exception as e:
                # Stop the component that failed as well: its start() may
                # have done real work (flagged itself running, spawned its
                # thread) before raising. Then unwind the rest in reverse.
                rolled_back = []
                for done_name, done in [(name, component)] + list(reversed(started)):
                    try:
                        done.stop()
                        rolled_back.append(done_name)
                    except Exception as stop_error:
                        logger.error(f"Market open rollback: could not stop {done_name}: {stop_error}")
                readiness.fail("market open", f"{name} failed to start: {e}")
                msg = (
                    f"Market open failed: {name} could not start ({e}). Live feed NOT running"
                    f" — stopped again: {', '.join(rolled_back) or 'nothing was running'}. "
                    f"Fix the cause, then re-run market open (or restart)."
                )
                logger.error(msg)
                notifier.risk_alert("Market open failed — feed not running", msg)
                raise RuntimeError(msg) from e
            started.append((name, component))

        if readiness.fully_ready:
            notifier.system_alert("Market Open", "Live scanning + tick storage started")
        else:
            notifier.risk_alert(
                "Market Open — DEGRADED (trading data unverified)",
                f"Scanning and candle collection started, but trading data is UNVERIFIED "
                f"({readiness.degradations()}) — new entries stay blocked. "
                f"A later successful warm-up clears this.",
            )

    def intraday_squareoff():
        """
        Square off intraday positions and close new entries for the rest of
        the day.

        LIVE/DUAL: exits are placed from the broker's own INTRADAY position
        rows, so positions opened by a previous process (before a restart)
        are covered. The broker's order book is checked first, so a repeat
        call or a 15:16 restart does not send a second exit while one is
        still working. Exits are *placed*, not confirmed filled — the alert
        says so, and a position with open quantity is never called closed.
        PAPER/DUAL: the paper book is in-memory only; paper positions from
        before a restart are unknown here and are reported, not counted.
        """
        report = order_manager.square_off_intraday()

        now = datetime.now(IST)
        started_mid_session = boot_time.date() == now.date() and boot_time.time() > MARKET_OPEN
        lines: list[str] = []
        problems: list[str] = []

        if report.mode in (TradingMode.PAPER, TradingMode.DUAL):
            lines.append(f"Paper: closed {len(report.paper_closed)} position(s)")
            if report.paper_failed:
                problems.append(
                    f"Paper: {len(report.paper_failed)} exit(s) failed: "
                    + "; ".join(r.message for r in report.paper_failed)
                )
            if started_mid_session:
                problems.append(
                    f"Paper: this process started at {boot_time:%H:%M}, after market open — "
                    f"paper positions opened before that are not tracked (paper state is "
                    f"in-memory only) and were NOT squared off here. Check the paper ledger."
                )

        if report.live_attempted:
            if report.live_lock_skipped:
                problems.append(f"LIVE: {report.live_lock_skipped}")
            if report.live_unreconciled:
                problems.append(
                    f"LIVE: {len(report.live_unreconciled)} entry submission(s) NOT reconciled — "
                    f"may be working or filled after square-off; NOT settled until the broker "
                    f"shows them terminal: " + "; ".join(e.describe() for e in report.live_unreconciled)
                )
            if report.live_unknown:
                problems.append(
                    f"LIVE: {len(report.live_unknown)} exit(s) SUBMITTED with outcome UNKNOWN — "
                    f"not rejected, not confirmed placed; not resent: "
                    + "; ".join(x.describe() for x in report.live_unknown)
                )
            if report.live_error:
                problems.append(
                    f"LIVE: broker could NOT be read ({report.live_error}) — NO exit orders "
                    f"placed. Check the broker's positions and order book and square off manually."
                )
            else:
                if report.live_cancelled:
                    lines.append(
                        f"LIVE: cancel requested for {len(report.live_cancelled)} open order(s) "
                        f"that could add exposure: "
                        + "; ".join(c.describe() for c in report.live_cancelled)
                    )
                if report.cancels_unclear:
                    problems.append(
                        f"LIVE: {len(report.cancels_unclear)} open order(s) could NOT be confirmed "
                        f"cancelled — they may still fill after square-off; cancel at the broker: "
                        + "; ".join(c.describe() for c in report.cancels_unclear)
                    )
                placed = report.live_placed
                if report.settled:
                    lines.append("LIVE: broker intraday book confirmed flat and settled")
                elif report.broker_flat:
                    lines.append("LIVE: broker position snapshot flat — NOT settled (see problems)")
                lines.append(
                    f"LIVE: {len(placed)} exit order(s) PLACED at broker from its intraday "
                    f"book — fills NOT confirmed"
                    + (": " + "; ".join(x.describe() for x in placed) if placed else "")
                    + (
                        f"; {report.live_rows_left_alone} non-intraday row(s) left alone"
                        if report.live_rows_left_alone else ""
                    )
                )
                if report.live_outstanding:
                    problems.append(
                        f"LIVE: {len(report.live_outstanding)} position(s) still open with an "
                        f"exit already working at the broker — no duplicate sent; watch for the "
                        f"fill or handle at the broker: "
                        + "; ".join(x.describe() for x in report.live_outstanding)
                    )
                if report.live_unclear:
                    problems.append(
                        f"LIVE: {len(report.live_unclear)} position(s) still open where the "
                        f"broker state could not be reconciled — NO order sent, handle at the "
                        f"broker: " + "; ".join(x.describe() for x in report.live_unclear)
                    )
                if report.live_rejected:
                    problems.append(
                        f"LIVE: {len(report.live_rejected)} exit(s) REJECTED by broker — "
                        f"position still open: "
                        + "; ".join(x.describe() for x in report.live_rejected)
                    )

        summary = " | ".join(lines) + " | new entries blocked until next pre-market"
        logger.info(f"Square-off: {summary}")
        for problem in problems:
            logger.warning(f"Square-off: {problem}")
        if problems:
            # Never report a successful square-off while the broker state is
            # unclear or something may still be open.
            notifier.risk_alert("Square Off — NOT confirmed complete, needs attention", "\n".join(problems))
            notifier.system_alert("Square Off — NOT confirmed complete", summary + " — see risk alert")
        else:
            notifier.system_alert("Square Off", summary)

        # Exits above are PLACED, not filled. Confirm later — in the
        # background, so this job returns now — whether the broker's book
        # actually went flat, and alert either way. Read-only: it never
        # sends another exit.
        if report.live_attempted:
            from trading.square_off_followup import SquareOffFollowUp

            SquareOffFollowUp(
                order_manager, notifier,
                delay_s=config_store.get_int("squareoff_followup_delay_s", 60) if config_store else 60,
                interval_s=config_store.get_int("squareoff_followup_interval_s", 60) if config_store else 60,
                max_attempts=config_store.get_int("squareoff_followup_attempts", 5) if config_store else 5,
            ).start()
            logger.info("Square-off: follow-up flat check scheduled in the background")

    def market_close():
        """Stop live scanning and flush all pending data."""
        ws_manager.stop()

        # Flush remaining ticks and candles to DB (spill what cannot be written)
        candles_pending = shutdown_live_components(candle_aggregator, tick_writer, notifier)
        if tick_writer:
            logger.info(f"TickWriter final stats: {tick_writer.stats}")
        alert = (
            candle_close_alert(candles_pending, candle_aggregator.stats)
            if candle_aggregator else None
        )

        if candles_pending:
            notifier.system_alert(
                "Market Closed",
                f"Live scanning stopped; {candles_pending} candle(s) NOT saved — see Candle Aggregator alert",
            )
        elif alert:
            notifier.system_alert(
                "Market Closed",
                "Live scanning stopped, data flushed — but a spill file could not be loaded; see Candle Aggregator alert",
            )
        else:
            notifier.system_alert("Market Closed", "Live scanning stopped, data flushed")

    def post_market():
        """
        Post-market analysis: bhavcopy download, data persistence,
        incremental candle update, and weekly aggregation.
        """
        # 1. Download and persist bhavcopy (delivery data)
        try:
            candles = bhavcopy.fetch()
            logger.info(f"Post-market bhavcopy: {len(candles)} stocks")

            # Persist to candles_1d with delivery data
            if ingestion and candles:
                stored = ingestion.store_bhavcopy(candles)
                logger.info(f"Bhavcopy persisted to DB: {stored} stocks")
        except Exception as e:
            logger.error(f"Post-market bhavcopy failed: {e}")

        # 2. Check coverage for ALL intervals and fill gaps (same as startup)
        # backfill_all_intervals() checks candle_coverage — if today's data
        # is missing, it fetches. If everything is up to date, it skips.
        if ingestion:
            try:
                backfill_stats = ingestion.backfill_all_intervals()
                for iv, stats in backfill_stats.items():
                    stored = stats.get("candles_stored", 0)
                    if stored > 0:
                        logger.info(f"Post-market {iv}: {stored:,} candles stored")
            except Exception as e:
                logger.error(f"Post-market coverage check failed: {e}")

        # 4. Clean up old live ticks (raw WebSocket data, keep last 3 days)
        # Candle data is NEVER deleted — it's permanent historical data.
        # Live ticks are disposable raw data that grows fast (~500K rows/day).
        if db:
            try:
                deleted = db.execute(
                    "DELETE FROM live_ticks WHERE timestamp < NOW() - INTERVAL '3 days'"
                )
                if deleted:
                    logger.info(f"Cleaned up {deleted} old live_ticks rows (keeping 3 days)")
            except Exception as e:
                logger.debug(f"Live ticks cleanup error: {e}")

        # 5. Compute technical indicators for ALL stocks
        # Pure CPU math, ~30-60 seconds, no API calls
        if ingestion:
            try:
                ind_stats = ingestion.compute_and_store_indicators()
                logger.info(
                    f"Post-market indicators: {ind_stats['computed']} computed, "
                    f"{ind_stats['skipped']} skipped, {ind_stats['errors']} errors"
                )
            except Exception as e:
                logger.error(f"Post-market indicator computation failed: {e}")

        # 6. Run post-market batch scanners (delivery, patterns, MTF)
        if _candle_cache:
            try:
                delivery_results = delivery_scanner.scan_all(_candle_cache)
                pattern_results = pattern_scanner.scan_all(_candle_cache, _indicator_cache)
                mtf_results = mtf_scanner.scan_all(_candle_cache, _indicator_cache)
                logger.info(
                    f"Post-market scanners: {len(delivery_results)} delivery, "
                    f"{len(pattern_results)} patterns, {len(mtf_results)} MTF signals"
                )

                # 7. Persist scanner results for next-day algorithm consumption
                if scanner_signal_store:
                    today = datetime.now(IST).date()
                    next_day = _get_next_trading_day(today)
                    signals_to_store = []

                    # Delivery scanner results
                    for r in delivery_results:
                        score_map = {"EXTREME": 0.80, "STRONG": 0.65, "MODERATE": 0.55, "WEAK": 0.45}
                        signals_to_store.append({
                            "symbol": r.symbol, "token": r.token,
                            "scanner_name": "delivery", "pattern": r.pattern,
                            "direction": r.direction,
                            "confidence": score_map.get(r.strength.value, 0.5),
                            "price": r.price, "strength": r.strength.value,
                            "details": {
                                "delivery_pct": round(r.delivery_pct, 1),
                                "avg_delivery_30d": round(r.avg_delivery_30d, 1),
                                "price_change_pct": round(r.price_change_pct, 2),
                            },
                        })

                    # Pattern scanner results
                    for r in pattern_results:
                        signals_to_store.append({
                            "symbol": r.symbol, "token": r.token,
                            "scanner_name": "pattern", "pattern": r.pattern_name,
                            "direction": r.direction,
                            "confidence": round(r.confidence, 3),
                            "price": r.price, "strength": r.strength.value,
                            "details": r.details,
                        })

                    # MTF scanner results
                    for r in mtf_results:
                        signals_to_store.append({
                            "symbol": r.symbol, "token": r.token,
                            "scanner_name": "mtf", "pattern": "mtf_confluence",
                            "direction": r.direction,
                            "confidence": round(r.confluence_score, 3),
                            "price": r.price, "strength": r.strength.value,
                            "details": {
                                "confluence_score": round(r.confluence_score, 2),
                                "timeframe_signals": r.timeframe_signals,
                            },
                        })

                    if signals_to_store:
                        stored = scanner_signal_store.insert_batch(
                            signals_to_store, scan_date=today, target_date=next_day,
                        )
                        logger.info(
                            f"Post-market: {stored}/{len(signals_to_store)} scanner signals "
                            f"persisted for {next_day}"
                        )

            except Exception as e:
                logger.error(f"Post-market scanner failed: {e}")

    def daily_summary():
        """Send daily P&L summary."""
        summary = paper_trader.get_daily_summary()
        notifier.daily_summary(
            date_str=datetime.now(IST).strftime("%Y-%m-%d"),
            realized_pnl=summary.realized_pnl,
            unrealized_pnl=summary.unrealized_pnl,
            total_trades=summary.total_trades,
            winning=summary.winning_trades,
            losing=summary.losing_trades,
            portfolio_value=paper_trader.portfolio_value,
            signals_generated=algorithm_manager._total_observations,
        )

    def midnight_reset():
        """Midnight tasks: re-login, reset counters."""
        try:
            session.login()
        except Exception as e:
            logger.error(f"Midnight re-login failed: {e}")

    # Register all jobs
    scheduler.register_pre_market(pre_market)
    scheduler.register_scanner_warmup(scanner_warmup)
    scheduler.register_market_open(market_open)
    scheduler.register_intraday_squareoff(intraday_squareoff)
    scheduler.register_market_close(market_close)
    scheduler.register_post_market(post_market)
    scheduler.register_daily_summary(daily_summary)
    scheduler.register_midnight_reset(midnight_reset)

    # Start the scheduler paused: every job's next run time is computed now,
    # but nothing fires until resume(). That gives the catch-up below a
    # stable view of which lifecycle jobs already passed today, with no
    # window in which the scheduler thread could fire one of them as well.
    scheduler.start(paused=True)

    now = datetime.now(IST)
    today = now.date()
    market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    is_weekday = now.weekday() < 5

    # Cron triggers only look forward: a job registered after its slot is
    # scheduled for the *next* day. So the session jobs whose next run is
    # not today were missed by this process.
    session_jobs = ("pre_market_boot", "scanner_warmup", "market_open", "intraday_squareoff")
    missed_session_jobs = []
    for job_name in session_jobs:
        next_run = scheduler.next_run_time(job_name)
        if next_run is None or next_run.date() != today:
            missed_session_jobs.append(job_name)

    scheduler.resume()

    # ── Startup Catch-Up: market hours — replay the missed morning jobs ──
    # A restart between 08:30 and 15:30 would otherwise sit idle all day:
    # no WebSocket subscription, no warm-up caches, tick writer and candle
    # aggregator never started. Replay in lifecycle order (pre-market sets
    # up state that warm-up needs) as ONE run_sync batch: the scheduler's
    # lifecycle lock is held across the batch, so a job that comes due
    # meanwhile (market_open at 09:15 while a slow warm-up is still
    # running) waits and runs once afterwards instead of overlapping.
    # Each job goes through the scheduler's wrapper, so a failure is logged
    # and isolated exactly like a scheduled run. Jobs still ahead today
    # (e.g. market_open when starting at 09:05) are left to the scheduler.
    #
    # After 15:15 the square-off is replayed too: the feed and candle
    # collection still run until close, but square-off re-establishes
    # "no new entries today", exits whatever the broker's intraday book
    # still holds (LIVE/DUAL) and reports that pre-restart *paper*
    # positions are unknown to this process.
    if is_weekday and now < market_close_time and missed_session_jobs:
        logger.info("=" * 60)
        logger.info(
            f"  STARTUP CATCH-UP: market hours ({now:%H:%M}), replaying missed "
            f"jobs: {', '.join(missed_session_jobs)}"
        )
        logger.info("=" * 60)
        if "intraday_squareoff" in missed_session_jobs:
            # Past square-off: shut the entry gate BEFORE the feed comes up.
            # market_open is replayed before intraday_squareoff, and a tick on
            # the very first second of the socket could otherwise open a trade.
            risk_manager.close_new_entries(
                f"process started at {now:%H:%M}, after square-off", key="square_off"
            )
        outcome = scheduler.run_sync(*missed_session_jobs, stop_on_failure=True)
        failed = [name for name, ok in outcome.items() if not ok]
        if failed:
            skipped = [j for j in missed_session_jobs if j not in outcome]
            msg = (
                f"Start-up aborted: {failed[0]} failed ({readiness.blockers()}); "
                f"skipped: {', '.join(skipped) or 'none'}. Live session NOT started — "
                f"fix the cause and restart, or trigger the jobs manually."
            )
            logger.error(msg)
            notifier.risk_alert("Start-up aborted — live session not started", msg)
        elif readiness.fully_ready:
            logger.info("  Startup catch-up complete — live session running")
        else:
            logger.warning(
                f"  Startup catch-up complete — session DEGRADED "
                f"({readiness.degradations()}); new entries blocked"
            )
        logger.info("=" * 60)
    elif is_weekday and now < market_close_time:
        logger.info("Startup check: before pre-market — all lifecycle jobs will run on schedule")

    # ── Startup at/after close: no live session today ──
    # Nothing starts the feed, and nothing may open a position either
    # (the API or a manual trigger could still reach the order path).
    if is_weekday and now >= market_close_time:
        risk_manager.close_new_entries(
            f"process started at {now:%H:%M}, after market close", key="after_close"
        )

    # ── Startup Catch-Up: Fetch data if market already closed ──
    # If we're starting at/after 3:30 PM IST and candle tables are empty
    # or stale, run backfill + indicators immediately instead of
    # waiting for the next scheduled post-market run.
    if is_weekday and now >= market_close_time and ingestion and db:
        try:
            logger.info("=" * 60)
            logger.info("  STARTUP CATCH-UP: Market closed, checking all intervals...")
            logger.info("=" * 60)

            # Backfill all candle intervals (smart — skips already-fetched per-stock)
            backfill_stats = ingestion.backfill_all_intervals()
            for iv, stats in backfill_stats.items():
                stored = stats.get("candles_stored", 0)
                skipped = stats.get("skipped", 0)
                if stored > 0:
                    logger.info(f"  Catch-up {iv}: {stored:,} candles stored, {skipped} skipped")
                else:
                    logger.info(f"  Catch-up {iv}: up to date ({skipped} stocks complete)")

            # 2. Compute technical indicators
            ind_stats = ingestion.compute_and_store_indicators()
            logger.info(
                f"  Catch-up indicators: {ind_stats['computed']} computed, "
                f"{ind_stats['skipped']} skipped, {ind_stats['errors']} errors"
            )

            # 3. Warm up scanners with fresh data (caches for post-market
            #    analysis; no session is started, so no pre-market dependency)
            scanner_warmup(require_pre_market=False)

            logger.info("  Startup catch-up complete!")
            logger.info("=" * 60)
        except Exception as e:
            logger.error(f"Startup catch-up failed: {e} — will retry at next scheduled run")
    elif not is_weekday:
        logger.info("Startup check: weekend — no catch-up needed")

    logger.info("=" * 60)
    logger.info("  TREND-INVESTORR RUNNING — Press Ctrl+C to stop")
    logger.info("=" * 60)

    # ── Graceful Shutdown ────────────────────────────────
    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received...")
        ws_manager.stop()
        scheduler.stop()
        # Persist live candles/ticks before the DB goes away; anything that
        # cannot be written is spilled for the next start.
        shutdown_live_components(candle_aggregator, tick_writer, notifier)
        if db:
            db.disconnect()
        audit.log(AuditEventType.SYSTEM_SHUTDOWN, {"reason": "signal", "signal": signum})
        notifier.system_alert("System Stopped", "Graceful shutdown completed")
        logger.info("Trend-Investorr stopped. Goodbye! 👋")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        shutdown_handler(signal.SIGINT, None)


if __name__ == "__main__":
    main()
