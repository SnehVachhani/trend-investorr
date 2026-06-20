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


def main():
    """Boot and run the Trend-Investorr trading system."""

    logger.info("=" * 60)
    logger.info("  TREND-INVESTORR v1.0 — Volume-First Trading Bot")
    logger.info("=" * 60)

    # ── Step 1: Configuration ────────────────────────────
    from config.settings import Settings
    from core.constants import IST

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
        vault = SecureVault(vault_path=settings.vault_dir)
        audit = AuditTrail(log_dir=settings.audit_dir)
        audit.log(AuditEventType.SYSTEM_STARTUP, {"version": "1.0", "mode": settings.trading_mode})
        # First-run: seed vault with credentials from .env
        if not vault.is_initialized:
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

    order_manager = OrderManager(
        risk_manager=risk_manager,
        circuit_breaker=circuit_breaker,
        paper_trader=paper_trader,
        audit=audit,
        mode=trading_mode,
        live_session=session if trading_mode != TradingMode.PAPER else None,
    )

    logger.info(f"Trading layer initialized: mode={trading_mode.value}, capital=₹{total_capital:,.0f}")

    # ── Step 9: Notifications ────────────────────────────
    from notifications.notifier import Notifier

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
        candle_aggregator = CandleAggregator(db=db, interval_min=5)
        logger.info("CandleAggregator initialized (5M candles)")

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
                order_result = order_manager.execute_signal(signal, analysis)
                if order_result and order_result.success:
                    notifier.trade_alert(
                        symbol=signal.symbol,
                        side=signal.signal_type.value,
                        quantity=order_result.filled_qty,
                        fill_price=order_result.avg_fill_price,
                        is_paper=(trading_mode != TradingMode.LIVE),
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

    bhavcopy = BhavcopyFetcher(cache_dir=settings.data_dir / "bhavcopy")
    scheduler = MarketScheduler(audit=audit)

    def pre_market():
        """Pre-market boot: reload instruments, refresh config, purge old data."""
        session.ensure_authenticated()
        instruments.load()
        if db:
            instruments.sync_to_db(db)
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

        # Purge old tick data (keep last 3 days)
        if tick_writer:
            tick_writer.purge_old_ticks(days=3)

        # Reset candle aggregator for new day
        if candle_aggregator:
            candle_aggregator.reset_daily()

        logger.info("Pre-market boot completed")

    def scanner_warmup():
        """
        Load historical volumes from DB and subscribe WebSocket tokens.

        Flow:
            1. Build observation router from watchlist bindings (via WatchlistManager)
            2. Subscribe watchlist stocks to WebSocket
            3. Check candle_coverage and backfill any gaps
            4. Load volumes + candle cache + indicators from DB into memory
        """
        nonlocal _candle_cache, _indicator_cache

        # 1. Build observation router from watchlist ↔ algorithm bindings
        #    WatchlistManager returns a WatchlistRouter (filtered) or
        #    PassthroughRouter (fallback) depending on DB bindings.
        if watchlist_manager:
            router = watchlist_manager.load_router()
            algorithm_manager.set_router(router)

        # 2. Subscribe stocks from active watchlists to WebSocket
        #    If watchlists have stocks, use them. Otherwise fall back to instruments[:500].
        all_tokens = []
        if watchlist_manager:
            all_tokens = watchlist_manager.get_subscribe_tokens()

        if not all_tokens:
            # Fallback: use first 500 instruments (original behavior)
            all_tokens = [s.token for s in instruments.get_all_by_exchange()][:500]
            logger.info(f"Scanner warm-up: no watchlist tokens found — using instruments fallback ({len(all_tokens)} tokens)")
        else:
            logger.info(f"Scanner warm-up: subscribing {len(all_tokens)} tokens from active watchlists")

        ws_manager.subscribe(all_tokens)

        # 2. Check coverage and backfill gaps (smart — skips already-complete stocks)
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
            except Exception as e:
                logger.error(f"Startup backfill failed: {e} — continuing with existing data")

            # Detect and fill intra-coverage holes (30 min budget)
            try:
                logger.info("Checking for intra-coverage holes...")
                from core.constants import CandleInterval as _CI
                for iv in [_CI.ONE_MINUTE, _CI.FIVE_MINUTE, _CI.FIFTEEN_MINUTE,
                           _CI.ONE_HOUR, _CI.ONE_DAY]:
                    hole_stats = ingestion.fill_holes(
                        interval=iv,
                        time_budget_minutes=30.0,
                    )
                    filled = hole_stats.get("holes_filled", 0)
                    still_missing = hole_stats.get("still_missing", 0)
                    if filled > 0 or still_missing > 0:
                        logger.info(
                            f"  Hole repair ({iv.value}): "
                            f"checked {hole_stats['stocks_checked']}, "
                            f"{filled} filled, {still_missing} remaining "
                            f"({hole_stats['stopped_reason']})"
                        )
            except Exception as e:
                logger.error(f"Hole repair failed: {e} — continuing with existing data")

        # 3. Load historical volume baselines + candle cache from DB
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
                logger.error(f"Warm-up data loading failed: {e}")

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
        else:
            logger.warning("Scanner warm-up: DB unavailable — running without historical baselines")

    def market_open():
        """Start live scanning and data persistence."""
        ws_manager.start()
        if tick_writer:
            tick_writer.start()
        if candle_aggregator:
            candle_aggregator.start()
        notifier.system_alert("Market Open", "Live scanning + tick storage started")

    def intraday_squareoff():
        """Square off all intraday positions."""
        results = order_manager.square_off_intraday()
        notifier.system_alert("Square Off", f"Closed {len(results)} intraday positions")

    def market_close():
        """Stop live scanning and flush all pending data."""
        ws_manager.stop()

        # Flush remaining ticks and candles to DB
        if tick_writer:
            tick_writer.stop()
            logger.info(f"TickWriter final stats: {tick_writer.stats}")
        if candle_aggregator:
            candle_aggregator.flush_all()
            candle_aggregator.stop()
            logger.info(f"CandleAggregator final stats: {candle_aggregator.stats}")

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

            # Fill intra-coverage holes across ALL intervals (30 min per interval)
            try:
                from core.constants import CandleInterval as _CI
                for iv in [_CI.ONE_MINUTE, _CI.FIVE_MINUTE, _CI.FIFTEEN_MINUTE,
                           _CI.ONE_HOUR, _CI.ONE_DAY]:
                    hole_stats = ingestion.fill_holes(
                        interval=iv,
                        time_budget_minutes=30.0,
                    )
                    filled = hole_stats.get("holes_filled", 0)
                    if filled > 0:
                        logger.info(f"  Post-market hole repair ({iv.value}): {hole_stats}")
            except Exception as e:
                logger.error(f"Post-market hole repair failed: {e}")

            # Refresh coverage counts from actual data
            if coverage_store:
                try:
                    for iv_enum in ["FIVE_MINUTE", "FIFTEEN_MINUTE", "ONE_HOUR", "ONE_DAY"]:
                        coverage_store.refresh_candle_counts(iv_enum)
                    logger.info("  Coverage counts refreshed from actual data")
                except Exception as e:
                    logger.error(f"Coverage refresh failed: {e}")

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

    # Start scheduler
    scheduler.start()

    # ── Startup Catch-Up: Run on EVERY boot ──
    # Unconditionally check for missing data and run the fast SQL aggregations
    # so that the system is fully synced every time the backend starts, 
    # exactly as required for morning prep.
    if ingestion and db:
        try:
            logger.info("=" * 60)
            logger.info("  STARTUP CATCH-UP: Checking intervals and running fast SQL aggregations...")
            logger.info("=" * 60)

            # Backfill all candle intervals (smart — skips already-fetched per-stock)
            # This now inherently includes the extremely fast local SQL aggregation.
            backfill_stats = ingestion.backfill_all_intervals()
            for iv, stats in backfill_stats.items():
                stored = stats.get("candles_stored", 0)
                skipped = stats.get("skipped", 0)
                source = stats.get("source", "api")
                if stored > 0:
                    logger.info(f"  Catch-up {iv}: {stored:,} candles stored, {skipped} skipped (via {source})")
                elif source == "local_aggregate":
                    logger.info(f"  Catch-up {iv}: Local aggregation complete.")
                else:
                    logger.info(f"  Catch-up {iv}: up to date ({skipped} stocks complete)")

            # Refresh coverage counts from actual data (fixes accumulated drift)
            if coverage_store:
                try:
                    for iv_enum in ["FIVE_MINUTE", "ONE_HOUR", "ONE_DAY"]:
                        coverage_store.refresh_candle_counts(iv_enum)
                    logger.info("  Coverage counts refreshed from actual data")
                except Exception as e:
                    logger.error(f"Coverage refresh failed: {e}")

            # 2. Compute technical indicators
            ind_stats = ingestion.compute_and_store_indicators()
            logger.info(
                f"  Catch-up indicators: {ind_stats['computed']} computed, "
                f"{ind_stats['skipped']} skipped, {ind_stats['errors']} errors"
            )

            # 3. Warm up scanners with fresh data
            scanner_warmup()

            logger.info("  Startup catch-up complete!")
            logger.info("=" * 60)
        except Exception as e:
            logger.error(f"Startup catch-up failed: {e} — will retry at next scheduled run")

    logger.info("=" * 60)
    logger.info("  TREND-INVESTORR RUNNING — Press Ctrl+C to stop")
    logger.info("=" * 60)

    # ── Graceful Shutdown ────────────────────────────────
    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received...")
        ws_manager.stop()
        scheduler.stop()
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
