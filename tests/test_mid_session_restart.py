"""
Mid-session restart: the process boots at 11:00 on a trading day.

main() registers the morning jobs as cron triggers (08:30 pre-market,
09:00 scanner warm-up, 09:15 market open) and starts the scheduler. Cron
triggers only look forward, so a job added at 11:00 whose slot is 09:00 is
scheduled for the *next* trading day. The "startup catch-up" block in main()
only covers the after-close case (now > 15:30); during the session it logs
"market still open — scheduler will handle data fetch" and does nothing.

The tests below boot the real ``main()`` against a fake clock and fake
external services (DB, broker session, WebSocket, scheduler, ...) and assert
the behaviour we want: by the time main() reaches its idle loop at 11:00 the
morning setup (pre-market → scanner warm-up → market open) has run exactly
once, in that order, and the live tick/candle pipeline is up.

Every fake appends to one ordered event log (``Harness.events``). The three
morning phases are recognised by side effects that only that job produces
(see ``PHASE_MARKERS``), so a job counts whether the scheduler fired it or
startup code called the closure directly — and running it twice shows up.

``test_real_scheduler_pushes_missed_morning_jobs_to_next_day`` pins the
APScheduler semantics that ``FakeMarketScheduler`` models, using the real
``MarketScheduler`` triggers, so the verdict of the restart tests does not
rest on an assumption about the fake.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from core.constants import IST, AnalysisAction, SignalType
from strategies.routing import PassthroughRouter
from tests.factories import make_analysis_result, make_order_request, make_position, make_signal, make_tick
from core.constants import TransactionType


# A Wednesday. 2026-03-04 11:00 IST — well inside the 09:15–15:30 session.
RESTART_AT = datetime(2026, 3, 4, 11, 0, tzinfo=IST)
assert RESTART_AT.weekday() == 2, "restart scenario must be on a trading weekday"

WATCHLIST_TOKENS = ["2885", "11536", "1594"]  # RELIANCE, TCS, INFY

# Side effects in main() that belong to exactly one lifecycle job.
PHASE_MARKERS = {
    "candle_aggregator.reset_daily": "pre_market",  # only pre_market() resets the aggregator
    "ws.subscribe": "scanner_warmup",               # only scanner_warmup() subscribes tokens
    "ws.start": "market_open",                      # only market_open() starts the feed
}
EXPECTED_MORNING_SEQUENCE = ["pre_market", "scanner_warmup", "market_open"]


def morning_phases(events: list[str]) -> list[str]:
    """Ordered list of morning phases that ran, derived from the event log."""
    return [PHASE_MARKERS[e] for e in events if e in PHASE_MARKERS]


def phase_counts(events: list[str]) -> dict[str, int]:
    phases = morning_phases(events)
    return {phase: phases.count(phase) for phase in EXPECTED_MORNING_SEQUENCE}


class Recording:
    """Mixin: ``_record("x")`` appends ``"<label>.x"`` to the shared event log."""

    _events: list[str] | None = None
    _label: str = ""

    def _record(self, what: str) -> None:
        if self._events is not None:
            self._events.append(f"{self._label}.{what}")


# ── Fake clock ───────────────────────────────────────────────


class FakeClock:
    """Single source of truth for 'now' shared by main() and the scheduler."""

    def __init__(self, start: datetime):
        self.current = start
        self.listeners: list = []  # called with the new time after every set()

    def now(self, tz=None) -> datetime:
        return self.current if tz is None else self.current.astimezone(tz)

    def set(self, when: datetime) -> None:
        assert when >= self.current, "the fake clock only moves forward"
        self.current = when
        for listener in list(self.listeners):
            listener(when)



def make_frozen_datetime(clock: FakeClock):
    """A datetime subclass whose now() reads from the fake clock.

    main() calls ``datetime.now(IST)`` and then uses ``.replace``,
    ``.weekday`` and ``.date`` on the result, so a real datetime subclass is
    used rather than a bare stub.
    """

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now(tz)

    return FrozenDatetime


# ── Fake scheduler ───────────────────────────────────────────


class FakeMarketScheduler:
    """
    In-process stand-in for MarketScheduler.

    Models APScheduler cron semantics for freshly added jobs: when the
    scheduler starts at time T, each job's first run is the first
    (hour, minute) slot that is >= T. Slots earlier in the day are pushed to
    the next day. Jobs that raise are recorded (the real wrapper swallows the
    error and logs it).

    Time passing *inside* a job (a fake service advancing the clock) models
    a slow job: the scheduler thread would fire anything that came due. Per
    the MarketScheduler contract, lifecycle jobs are serialised and a
    ``run_sync`` batch is atomic, so such jobs are deferred until the running
    job / batch finishes — never skipped.
    """

    def __init__(self, clock: FakeClock, events: list[str]):
        self._clock = clock
        self._events = events
        self.jobs: dict[str, tuple[int, int, object]] = {}
        self.started_at: datetime | None = None
        self.paused = False
        self.stopped = False
        self.fired: list[tuple[str, datetime]] = []
        self.errors: list[tuple[str, Exception]] = []
        self.sync_batches: list[list[str]] = []  # every run_sync() call, in order
        self._next_run: dict[str, datetime] = {}
        self._holding = 0  # >0 while a job or a run_sync batch holds the lifecycle lock
        clock.listeners.append(self._on_clock)

    # registration API used by main()
    def register_pre_market(self, fn):
        self._add("pre_market_boot", 8, 30, fn)

    def register_scanner_warmup(self, fn):
        self._add("scanner_warmup", 9, 0, fn)

    def register_market_open(self, fn):
        self._add("market_open", 9, 15, fn)

    def register_intraday_squareoff(self, fn):
        self._add("intraday_squareoff", 15, 15, fn)

    def register_market_close(self, fn):
        self._add("market_close", 15, 30, fn)

    def register_post_market(self, fn):
        self._add("post_market_analysis", 16, 0, fn)

    def register_daily_summary(self, fn):
        self._add("daily_summary", 17, 0, fn)

    def register_midnight_reset(self, fn):
        self._add("midnight_reset", 0, 0, fn)

    def register_periodic(self, name, fn, minutes=5, description=""):
        # Not used by main(); kept so registration never blows up.
        self.jobs[name] = (-1, minutes, fn)

    def _add(self, name, hour, minute, fn):
        self.jobs[name] = (hour, minute, fn)

    # lifecycle
    def start(self, paused=False):
        self.started_at = self._clock.now(IST)
        self.paused = paused
        self._events.append(f"scheduler.start(paused={paused})")
        for name, (hour, minute, _fn) in self.jobs.items():
            if hour < 0:  # periodic jobs are not part of this scenario
                continue
            slot = self.started_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot < self.started_at:
                slot += timedelta(days=1)
            self._next_run[name] = slot

    def resume(self):
        self.paused = False
        self._events.append("scheduler.resume")

    def stop(self, wait=True):
        self.stopped = True

    def next_run_time(self, job_name):
        return self._next_run.get(job_name)

    def run_sync(self, *job_names, stop_on_failure=False):
        """Run jobs back-to-back as one atomic batch (lifecycle lock held throughout)."""
        self.sync_batches.append(list(job_names))
        outcome: dict[str, bool] = {}
        self._holding += 1
        try:
            for name in job_names:
                ok = self._fire(name, self.jobs[name][2])
                outcome[name] = ok
                if not ok and stop_on_failure:
                    break
        finally:
            self._holding -= 1
        self._fire_due()
        return outcome

    def run_now(self, job_name):
        _h, _m, fn = self.jobs[job_name]
        self._fire(job_name, fn)

    # test driver
    def run_until(self, when: datetime) -> list[str]:
        """Advance the clock to ``when``; due jobs fire via the clock listener."""
        assert self.started_at is not None, "scheduler was never started"
        assert not self.paused, "scheduler is still paused — nothing can fire"
        before = len(self.fired)
        while True:
            due = [slot for slot in self._next_run.values() if slot <= when]
            if not due:
                break
            self._clock.set(min(due))  # listener fires everything due at that slot
        self._clock.set(when)
        return [name for name, _slot in self.fired[before:]]

    # modelling of the scheduler thread
    def _on_clock(self, now: datetime) -> None:
        if self.started_at is None or self.paused or self._holding:
            return  # deferred: fired when the running job / batch releases the lock
        self._fire_due()

    def _fire_due(self) -> None:
        while True:
            now = self._clock.now(IST)
            due = [(slot, name) for name, slot in self._next_run.items() if slot <= now]
            if not due:
                return
            slot, name = min(due)
            self._next_run[name] = slot + timedelta(days=1)
            self._fire(name, self.jobs[name][2], scheduled_for=slot)

    def _fire(self, name, fn, scheduled_for=None) -> bool:
        self.fired.append((name, scheduled_for or self._clock.now(IST)))
        self._events.append(f"scheduler.fire:{name}")
        self._holding += 1  # each job runs under the lifecycle lock
        ok = True
        try:
            fn()
        except Exception as e:  # mirror MarketScheduler._wrap_job
            self.errors.append((name, e))
            ok = False
        finally:
            self._holding -= 1
        self._events.append(f"scheduler.done:{name}" if ok else f"scheduler.failed:{name}")
        if not self._holding:
            self._fire_due()
        return ok

    @property
    def next_runs(self) -> dict[str, datetime]:
        return dict(self._next_run)


# ── Fake services ────────────────────────────────────────────


class FakeSettings:
    def __init__(self, root: Path):
        self.vault_dir = root / "vault"
        self.audit_dir = root / "audit"
        self.data_dir = root / "data"
        self.VAULT_MASTER_KEY = ""
        self.trading_mode = "PAPER"
        self.db_host, self.db_port, self.db_name = "localhost", 5432, "test"
        self.db_user, self.db_password = "test", ""
        self.db_pool_min, self.db_pool_max = 1, 2


class FakeVault:
    def __init__(self, vault_path, master_key=""):
        pass

    def setup_from_env(self, settings):
        return True

    def get_credential(self, key):
        return None


class FakeAuditTrail:
    def __init__(self, log_dir, db_writer=None):
        self.events = []

    def log(self, event_type, details, **kwargs):
        self.events.append((event_type, details))


class FakeDatabase:
    def __init__(self, **kwargs):
        self.connected = False
        self.inserted_candles: list[tuple[str, tuple]] = []  # (interval, row) via FakeCandleStore

    def connect(self):
        self.connected = True

    def run_migrations(self):
        pass

    def disconnect(self):
        self.connected = False

    def execute(self, *args, **kwargs):
        return 0

    def health_check(self):
        return True


class FakeStore:
    def __init__(self, db):
        self._db = db


class FakeAppConfigStore(FakeStore):
    """Returns the caller's default for every key."""

    def reload(self):
        pass

    def get(self, key, default=None):
        return default

    def get_float(self, key, default=0.0):
        return default

    def get_int(self, key, default=0):
        return default

    def get_bool(self, key, default=False):
        return default


class FakeCandleStore(FakeStore):
    """What CandleAggregator writes through: records rows on the shared fake DB."""

    def insert_batch(self, interval, rows):
        self._db.inserted_candles.extend((getattr(interval, "value", str(interval)), r) for r in rows)
        return len(rows)


KNOWN_STOCK_IDS = {"2885": 1, "11536": 2, "1594": 3, "9999": 77}  # 9999: no longer on any watchlist


class FakeStockStore(FakeStore):
    def get_ids_by_tokens(self, tokens, exchange="NSE"):
        return {t: KNOWN_STOCK_IDS[t] for t in tokens if t in KNOWN_STOCK_IDS}

    def get_by_symbol(self, symbol):
        return None


class FakeScannerSignalStore(FakeStore):
    def get_active_for_date(self, target_date):
        return []

    def mark_consumed(self, ids):
        return 0

    def expire_old(self, before_date):
        return 0

    def insert_batch(self, signals, scan_date, target_date):
        return 0


class FakeWatchlistManager:
    def __init__(self, watchlist_store, binding_store):
        pass

    def get_subscribe_tokens(self):
        return list(WATCHLIST_TOKENS)

    def load_router(self):
        return PassthroughRouter()


class FakeSession:
    def __init__(self, vault, audit):
        self.logins = 0

    def login(self):
        self.logins += 1

    def ensure_authenticated(self):
        pass

    @property
    def is_authenticated(self):
        return True


class FakeInstrumentLoader:
    def __init__(self, cache_dir):
        pass

    def load(self, force_refresh=False):
        return 0

    def sync_to_db(self, db):
        return 0

    def get_all_by_exchange(self, exchange=None):
        return []

    def __len__(self):
        return 0


class FakeHistoricalFetcher:
    def __init__(self, session):
        pass


class FakeBhavcopyFetcher:
    def __init__(self, cache_dir):
        pass

    def fetch(self):
        return []


class FakeIngestion(Recording):
    _label = "ingestion"

    # Set by a test to make the (normally instant) backfill "take" until this time.
    backfill_finishes_at: datetime | None = None
    clock: FakeClock | None = None

    def __init__(self, historical_fetcher, candle_store, stock_store, coverage_store=None):
        self.calls: list[str] = []

    def set_indicator_store(self, store):
        pass

    def backfill_all_intervals(self, *args, **kwargs):
        self.calls.append("backfill_all_intervals")
        self._record("backfill_all_intervals")
        if self.backfill_finishes_at is not None and self.clock is not None:
            self.clock.set(self.backfill_finishes_at)  # slow warm-up: time passes in here
        return {"ONE_DAY": {"candles_stored": 0, "skipped": 3}}

    def load_warmup_data(self, days=90):
        self.calls.append("load_warmup_data")
        self._record("load_warmup_data")
        return {}, {}, {}

    def compute_and_store_indicators(self, *args, **kwargs):
        self.calls.append("compute_and_store_indicators")
        self._record("compute_and_store_indicators")
        return {"computed": 0, "skipped": 0, "errors": 0}

    def store_bhavcopy(self, candles):
        return 0


class FakeWebSocketManager(Recording):
    _label = "ws"

    def __init__(self, session, audit, feed_mode=None):
        self.subscribed: list[str] = []
        self.started = False
        self.stopped = False
        self.callbacks = []

    def on_tick(self, callback):
        self.callbacks.append(callback)

    def subscribe(self, tokens):
        self.subscribed.extend(tokens)
        self._record("subscribe")

    @property
    def running(self):
        return self.started and not self.stopped

    on_start = None  # optional callable, invoked inside start() — "the first tick"

    def start(self):
        self.started = True
        self._record("start")
        if self.on_start is not None:
            self.on_start()

    def stop(self):
        self.stopped = True
        self._record("stop")


class FakeTickWriter(Recording):
    _label = "tick_writer"

    def __init__(self, db, token_to_stock_id=None):
        self.started = False
        self.stopped = False

    on_start = None  # optional callable, invoked inside start() — "the first tick"

    def start(self):
        self.started = True
        self._record("start")
        if self.on_start is not None:
            self.on_start()

    def stop(self):
        self.stopped = True
        self._record("stop")

    def purge_old_ticks(self, days=3):
        self._record("purge_old_ticks")
        return 0

    def buffer(self, tick):
        self._record(f"buffer(started={self.running})")

    @property
    def running(self):
        return self.started and not self.stopped

    @property
    def stats(self):
        return {}


class FakeCandleAggregator(Recording):
    _label = "candle_aggregator"

    def __init__(self, db, interval_min=5, spill_path=None):
        self.started = False
        self.stopped = False
        self.token_mapping: dict[str, int] = {}

    on_start = None  # optional callable, invoked inside start() — "the first tick"

    def start(self):
        self.started = True
        self._record("start")
        if self.on_start is not None:
            self.on_start()

    def stop(self):
        self.stopped = True
        self._record("stop")
        return 0

    def reset_daily(self):
        self._record("reset_daily")
        return 0

    def recover_pending(self):
        return 0

    def set_token_mapping(self, mapping):
        self.token_mapping = dict(mapping)
        self._record("set_token_mapping")

    def flush_pending(self):
        return 0

    def on_tick(self, tick):
        self._record(f"on_tick(started={self.running})")

    @property
    def running(self):
        return self.started and not self.stopped

    @property
    def pending_tokens(self):
        return set()

    @property
    def pending_candles(self):
        return 0

    @property
    def stats(self):
        return {}


# ── Harness ──────────────────────────────────────────────────


class MainIdle(Exception):
    """Raised from the fake time.sleep once main() reaches its idle loop."""


class Harness:
    """Holds the fake instances main() created so tests can inspect them."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.events: list[str] = []  # ordered log of every recorded fake call
        self.scheduler: FakeMarketScheduler | None = None
        self.ws: FakeWebSocketManager | None = None
        self.tick_writer: FakeTickWriter | None = None
        self.candle_aggregator: FakeCandleAggregator | None = None
        self.ingestion: FakeIngestion | None = None
        self.order_manager = None  # the real OrderManager main() built
        self.db: FakeDatabase | None = None
        self.alerts: list[tuple[str, str, str]] = []  # (kind, title, message) from the real Notifier

        self.order_at_feed_start = None  # result of a trade attempted the moment the socket starts

    # Helpers for "can a trade be opened right now?"
    def try_open_long(self, symbol="RELIANCE", token="2885"):
        """Push a fresh BUY entry through the real Analyze → Risk → Execute path."""
        return self.order_manager.execute_signal(
            make_signal(symbol=symbol, token=token, signal_type=SignalType.BUY, score=0.7),
            make_analysis_result(action=AnalysisAction.BUY, confidence=0.8),
        )

    def try_sell(self, symbol="RELIANCE", token="2885"):
        """Push a SELL signal through the same path (exit if held, else a short entry)."""
        return self.order_manager.execute_signal(
            make_signal(symbol=symbol, token=token, signal_type=SignalType.SELL, score=0.7),
            make_analysis_result(action=AnalysisAction.SELL, confidence=0.8),
        )


def _capture(harness: Harness, attr: str, cls):
    """Subclass ``cls`` so the instance main() builds lands on the harness."""

    def __init__(self, *args, **kwargs):
        self._events = harness.events
        cls.__init__(self, *args, **kwargs)
        setattr(harness, attr, self)

    return type(f"Captured{cls.__name__}", (cls,), {"__init__": __init__})


def _fake_module(name: str, **objects) -> types.ModuleType:
    module = types.ModuleType(name)
    for attr, value in objects.items():
        setattr(module, attr, value)
    return module


SETUP_FAILURES = {
    # name -> (unused, "label.method", error) — the fake method raises when called
    "auth": (None, "session.ensure_authenticated", "broker session expired"),
    "pre_market": (None, "instruments.load", "instrument master download failed"),
    "warmup_subscribe": (None, "ws.subscribe", "websocket subscribe failed"),
    "warmup_data": (None, "ingestion.load_warmup_data", "warm-up data load failed"),
    "backfill": (None, "ingestion.backfill_all_intervals", "historical API down"),
    "writer_start": (None, "tick_writer.start", "live_ticks table locked"),
    "aggregator_start": (None, "candle_aggregator.start", "spill file unreadable"),
    # start() does its work (flags itself running, would have spawned its thread) and THEN raises
    "writer_start_partial": ("partial", "tick_writer.start", "flush thread died right after start"),
    "aggregator_start_partial": ("partial", "candle_aggregator.start", "flush thread died right after start"),
}


def boot_main_at(
    monkeypatch,
    tmp_path: Path,
    when: datetime,
    *,
    warmup_runs_until: datetime | None = None,
    fail=None,
    real_candle_aggregator: bool = False,
) -> Harness:
    """
    Run main() with the clock frozen at ``when`` until it reaches its idle loop.

    ``warmup_runs_until`` makes scanner warm-up slow: its backfill step
    advances the clock to that time, so anything scheduled in between comes
    due while warm-up is still running.

    ``fail`` injects a set-up failure (see SETUP_FAILURES): the named fake
    method raises every time it is called.
    """
    clock = FakeClock(when)
    harness = Harness(clock)
    monkeypatch.setattr(FakeIngestion, "clock", clock)
    monkeypatch.setattr(FakeIngestion, "backfill_finishes_at", warmup_runs_until)

    for name in ([fail] if isinstance(fail, str) else list(fail or [])):
        kind, target, error = SETUP_FAILURES[name]
        label, method = target.split(".")
        cls = {"instruments": FakeInstrumentLoader, "ws": FakeWebSocketManager,
               "ingestion": FakeIngestion, "session": FakeSession,
               "tick_writer": FakeTickWriter, "candle_aggregator": FakeCandleAggregator}[label]
        original = getattr(cls, method)

        def boom(self, *args, _target=target, _error=error, _kind=kind, _orig=original, **kwargs):
            if _kind == "partial":
                _orig(self, *args, **kwargs)  # the real work happens (component is now running)...
            harness.events.append(f"{_target}:raises")
            raise RuntimeError(_error)  # ...and then it fails

        monkeypatch.setattr(cls, method, boom)

    def scheduler_factory(audit=None):
        harness.scheduler = FakeMarketScheduler(clock, harness.events)
        return harness.scheduler

    fakes = {
        "config.settings": _fake_module(
            "config.settings", Settings=lambda: FakeSettings(tmp_path)
        ),
        "core.security.vault": _fake_module("core.security.vault", SecureVault=FakeVault),
        "core.security.audit": _fake_module("core.security.audit", AuditTrail=FakeAuditTrail),
        "storage.database": _fake_module(
            "storage.database", Database=_capture(harness, "db", FakeDatabase)
        ),
        "storage.models": _fake_module(
            "storage.models",
            AppConfigStore=FakeAppConfigStore,
            CandleStore=FakeCandleStore,
            CandleCoverageStore=FakeStore,
            ScannerSignalStore=FakeScannerSignalStore,
            StockStore=FakeStockStore,
            TechnicalIndicatorStore=FakeStore,
            WatchlistStore=FakeStore,
            WatchlistAlgorithmBindingStore=FakeStore,
            FilterProfileStore=FakeStore,
        ),
        "core.watchlist_manager": _fake_module(
            "core.watchlist_manager", WatchlistManager=FakeWatchlistManager
        ),
        "data.auth.smartapi_auth": _fake_module(
            "data.auth.smartapi_auth", SmartAPISession=FakeSession
        ),
        "data.fetchers.instrument_loader": _fake_module(
            "data.fetchers.instrument_loader", InstrumentLoader=FakeInstrumentLoader
        ),
        "data.fetchers.historical_fetcher": _fake_module(
            "data.fetchers.historical_fetcher", HistoricalFetcher=FakeHistoricalFetcher
        ),
        "data.fetchers.bhavcopy_fetcher": _fake_module(
            "data.fetchers.bhavcopy_fetcher", BhavcopyFetcher=FakeBhavcopyFetcher
        ),
        "data.data_ingestion": _fake_module(
            "data.data_ingestion",
            DataIngestionService=_capture(harness, "ingestion", FakeIngestion),
        ),
        "data.streams.websocket_manager": _fake_module(
            "data.streams.websocket_manager",
            WebSocketManager=_capture(harness, "ws", FakeWebSocketManager),
        ),
        "data.streams.tick_writer": _fake_module(
            "data.streams.tick_writer",
            TickWriter=_capture(harness, "tick_writer", FakeTickWriter),
        ),
        "data.streams.candle_aggregator": _fake_module(
            "data.streams.candle_aggregator",
            CandleAggregator=_capture(harness, "candle_aggregator", FakeCandleAggregator),
        ),
        "scheduler.market_scheduler": _fake_module(
            "scheduler.market_scheduler", MarketScheduler=scheduler_factory
        ),
    }
    if real_candle_aggregator:
        # Exercise the real spill-recovery path: real class, real spill
        # file under the fake settings' data dir, fake storage underneath.
        del fakes["data.streams.candle_aggregator"]
        import data.streams.candle_aggregator as aggregator_module

        monkeypatch.setattr(
            aggregator_module, "CandleAggregator",
            _capture(harness, "candle_aggregator", aggregator_module.CandleAggregator),
        )

    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)

    # Real trading layer and notifier, but keep a handle on the instances.
    import trading.order_manager as order_manager_module
    import trading.risk_manager as risk_manager_module
    import notifications.notifier as notifier_module

    monkeypatch.setattr(
        order_manager_module, "OrderManager",
        _capture(harness, "order_manager", order_manager_module.OrderManager),
    )

    class RecordingNotifier(notifier_module.Notifier):
        def system_alert(self, title, message, *args, **kwargs):
            harness.alerts.append(("system", title, message))
            harness.events.append(f"notifier.system_alert:{title}")
            return super().system_alert(title, message, *args, **kwargs)

        def risk_alert(self, title, details):
            harness.alerts.append(("risk", title, details))
            harness.events.append(f"notifier.risk_alert:{title}")
            return super().risk_alert(title, details)

    monkeypatch.setattr(notifier_module, "Notifier", RecordingNotifier)

    # The instant the socket starts, a tick could produce a signal: model the
    # worst case by attempting a BUY entry from inside ws.start().
    def trade_on_first_tick():
        harness.events.append("first_tick.try_open_long")
        harness.order_at_feed_start = harness.try_open_long()
        # ...and the tick itself goes through main's on_tick_handler, which
        # hands it to the tick writer and the candle aggregator.
        for callback in harness.ws.callbacks:
            callback(make_tick(token=WATCHLIST_TOKENS[0]))

    monkeypatch.setattr(FakeWebSocketManager, "on_start", staticmethod(trade_on_first_tick))

    # Fake clock (main and the risk manager's market-hours gate), no real
    # signal handlers, and break out of the idle loop.
    frozen = make_frozen_datetime(clock)
    monkeypatch.setattr(main, "datetime", frozen)
    monkeypatch.setattr(risk_manager_module, "datetime", frozen)
    monkeypatch.setattr(
        main, "signal", SimpleNamespace(signal=lambda *a, **k: None, SIGINT=2, SIGTERM=15)
    )

    def fake_sleep(_seconds):
        raise MainIdle()

    monkeypatch.setattr(main, "time", SimpleNamespace(sleep=fake_sleep))

    with pytest.raises(MainIdle):
        main.main()

    assert harness.scheduler is not None and harness.scheduler.started_at == when
    assert harness.ws is not None
    assert harness.tick_writer is not None
    assert harness.candle_aggregator is not None
    assert harness.ingestion is not None
    assert harness.order_manager is not None
    assert harness.db is not None
    return harness


@pytest.fixture
def restarted_at_11(monkeypatch, tmp_path) -> Harness:
    """main() booted at 11:00 IST on a trading day, scheduler running."""
    return boot_main_at(monkeypatch, tmp_path, RESTART_AT)


# ── Root cause / fake fidelity: real cron triggers vs. restart time ──

MORNING_JOBS = ("pre_market_boot", "scanner_warmup", "market_open")


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return RESTART_AT.replace(hour=hour, minute=minute, second=second)


@pytest.mark.parametrize(
    "restart_time, runs_today",
    [
        (at(8, 45), {"scanner_warmup", "market_open"}),
        (at(9, 5), {"market_open"}),
        (at(9, 15, 0), {"market_open"}),  # slot == now → still fires today
        (at(9, 16), set()),
        (at(11, 0), set()),
    ],
    ids=["08:45", "09:05", "09:15:00", "09:16", "11:00"],
)
def test_real_scheduler_pushes_missed_morning_jobs_to_next_day(restart_time, runs_today):
    """
    Pins the APScheduler behaviour that FakeMarketScheduler models: a job
    added at time T first fires at the first slot >= T, so slots already
    passed today land on the next day. Uses the real MarketScheduler
    triggers (never started, so no thread is spawned).
    """
    from scheduler.market_scheduler import MarketScheduler

    scheduler = MarketScheduler(audit=None)
    noop = lambda: None  # noqa: E731
    scheduler.register_pre_market(noop)
    scheduler.register_scanner_warmup(noop)
    scheduler.register_market_open(noop)
    scheduler.register_intraday_squareoff(noop)
    scheduler.register_market_close(noop)

    next_run = {
        job.id: job.trigger.get_next_fire_time(None, restart_time)
        for job in scheduler._scheduler.get_jobs()
    }
    today = restart_time.date()

    for name in MORNING_JOBS:
        expected = today if name in runs_today else today + timedelta(days=1)
        assert next_run[name].date() == expected, f"{name} from {restart_time:%H:%M:%S}"

    # Afternoon jobs are always still ahead in these scenarios.
    for name in ("intraday_squareoff", "market_close"):
        assert next_run[name].date() == today, name


def test_real_scheduler_paused_start_exposes_next_runs_and_run_sync_is_synchronous():
    """
    The mechanism main() relies on, against the real MarketScheduler:
    start(paused=True) computes next_run_time for every job without firing
    anything; run_sync() runs a job in the calling thread through the normal
    wrapper (errors are isolated); resume() then hands over to the thread.
    """
    import threading
    from scheduler.market_scheduler import MarketScheduler

    calls: list[tuple[str, str]] = []
    scheduler = MarketScheduler(audit=None)
    scheduler.register_pre_market(lambda: calls.append(("pre_market", threading.current_thread().name)))
    scheduler.register_scanner_warmup(lambda: 1 / 0)  # a failing job must not raise

    scheduler.start(paused=True)
    try:
        assert scheduler.next_run_time("pre_market_boot") is not None
        assert scheduler.next_run_time("scanner_warmup") is not None
        assert scheduler.next_run_time("no_such_job") is None

        # One batch: the failing job (1/0) is swallowed by the wrapper and
        # reported as False, the unknown one is skipped, the batch completes.
        ran = scheduler.run_sync("pre_market_boot", "scanner_warmup", "no_such_job")
        assert ran == {"pre_market_boot": True, "scanner_warmup": False}
        assert calls == [("pre_market", threading.current_thread().name)]

        # stop_on_failure: nothing after the failed job runs.
        calls.clear()
        ran = scheduler.run_sync("scanner_warmup", "pre_market_boot", stop_on_failure=True)
        assert ran == {"scanner_warmup": False}
        assert calls == []

        scheduler.resume()
        assert calls == []  # nothing extra fired on resume
    finally:
        scheduler.stop(wait=True)


# ── Behaviour we want after a mid-session restart ────────────
#
# All checks below look at the state the moment main() reaches its idle loop
# (the fake clock is still 11:00 — nothing has been advanced).


def test_restart_at_11_runs_morning_setup_once_in_order(restarted_at_11: Harness):
    """
    Booting at 11:00 must bring the process to the same state it would be in
    had it been running since 08:30: pre-market, scanner warm-up and market
    open each ran exactly once, in that order, before main() went idle.
    """
    h = restarted_at_11
    assert h.clock.now(IST) == RESTART_AT  # nothing advanced; this is the idle-loop state

    assert morning_phases(h.events) == EXPECTED_MORNING_SEQUENCE, (
        f"morning setup at idle loop: {phase_counts(h.events)}; "
        f"events so far: {h.events}; next scheduled runs: {h.scheduler.next_runs}"
    )


def test_restart_at_11_live_pipeline_is_up_at_idle_loop(restarted_at_11: Harness):
    """
    By the time main() goes idle at 11:00, the WebSocket is subscribed and
    running, warm-up data is loaded, and the tick writer / 5-minute candle
    aggregator are started — otherwise nothing is scanned or collected today.
    """
    h = restarted_at_11
    assert h.clock.now(IST) == RESTART_AT

    assert sorted(h.ws.subscribed) == sorted(WATCHLIST_TOKENS), (
        f"WebSocket not subscribed at idle loop; events: {h.events}"
    )
    assert "load_warmup_data" in h.ingestion.calls, "warm-up data never loaded"
    assert set(h.candle_aggregator.token_mapping) == set(WATCHLIST_TOKENS)
    assert h.ws.started, "WebSocket never started"
    assert h.tick_writer.started, "tick writer never started"
    assert h.candle_aggregator.started, "5-minute candle aggregator never started"


def test_restart_at_11_morning_setup_is_not_repeated_later_today(restarted_at_11: Harness):
    """
    Whatever runs the morning setup at startup must not also leave it queued
    for later today: letting the scheduler run through the rest of the day
    must leave each phase at exactly one execution.
    """
    h = restarted_at_11
    at_idle = phase_counts(h.events)

    end_of_day = RESTART_AT.replace(hour=23, minute=59)
    fired = h.scheduler.run_until(end_of_day)

    assert h.scheduler.errors == []
    assert phase_counts(h.events) == {phase: 1 for phase in EXPECTED_MORNING_SEQUENCE}, (
        f"phase counts at idle loop: {at_idle}; at end of day: {phase_counts(h.events)}; "
        f"jobs fired by scheduler after 11:00: {fired}"
    )
    # And the order must still be the canonical one (no warm-up after open, etc.)
    assert morning_phases(h.events) == EXPECTED_MORNING_SEQUENCE


# ── Restarts around 09:00 / 09:15 ────────────────────────────
#
# Only the jobs the scheduler will no longer run today are replayed at
# startup; the rest run on schedule. Either way each phase runs exactly once
# and in lifecycle order, and the feed is up by 09:15.


@pytest.mark.parametrize(
    "restart_time, replayed_batch, at_idle",
    [
        (at(8, 45), ["pre_market_boot"], ["pre_market"]),
        (at(9, 5), ["pre_market_boot", "scanner_warmup"], ["pre_market", "scanner_warmup"]),
        # 09:15:00 sharp: market_open is due at once; the scheduler fires it as
        # soon as the replay batch releases the lifecycle lock — after warm-up.
        (at(9, 15, 0), ["pre_market_boot", "scanner_warmup"], EXPECTED_MORNING_SEQUENCE),
        (at(9, 16), list(MORNING_JOBS), EXPECTED_MORNING_SEQUENCE),
    ],
    ids=["08:45", "09:05", "09:15:00", "09:16"],
)
def test_restart_near_open_replays_only_missed_jobs_then_scheduler_runs_the_rest(
    monkeypatch, tmp_path, restart_time, replayed_batch, at_idle
):
    h = boot_main_at(monkeypatch, tmp_path, restart_time)

    # 1. Only the already-missed jobs are replayed, as one atomic batch.
    assert h.clock.now(IST) == restart_time
    assert h.scheduler.sync_batches == [replayed_batch]
    assert morning_phases(h.events) == at_idle, (
        f"at idle loop ({restart_time:%H:%M:%S}): phases {morning_phases(h.events)}; "
        f"events: {h.events}"
    )
    assert h.scheduler.errors == []
    assert h.ws.started is ("market_open" in at_idle)

    # 2. The scheduler runs whatever is still ahead, on time, and the feed is up by 09:15.
    fired = h.scheduler.run_until(max(restart_time, at(9, 15, 0)))
    assert h.scheduler.errors == []
    assert morning_phases(h.events) == EXPECTED_MORNING_SEQUENCE, (
        f"after 09:15: phases {morning_phases(h.events)}; scheduler fired {fired}"
    )
    assert sorted(h.ws.subscribed) == sorted(WATCHLIST_TOKENS)
    assert h.ws.started and h.tick_writer.started and h.candle_aggregator.started

    # 3. Nothing is repeated during the rest of the day.
    h.scheduler.run_until(restart_time.replace(hour=23, minute=59))
    assert h.scheduler.errors == []
    assert phase_counts(h.events) == {phase: 1 for phase in EXPECTED_MORNING_SEQUENCE}


# ── Slow warm-up running across 09:15 ────────────────────────


def test_real_scheduler_market_open_waits_for_running_warmup_and_is_not_misfired():
    """
    Against the real MarketScheduler and real threads: warm-up is replayed
    synchronously and held open while 09:15 arrives. market_open must wait
    for warm-up to finish, then run exactly once — and must not be dropped
    as a misfire while it waits (the grace period is shortened to 1s and
    warm-up is held for 2s so a merely-queued job would visibly be lost).
    """
    import threading
    import time as real_time
    from apscheduler.events import EVENT_JOB_MISSED
    from scheduler.market_scheduler import MarketScheduler

    log: list[str] = []
    warmup_started = threading.Event()
    release_warmup = threading.Event()

    def warmup():
        log.append("warmup:start")
        warmup_started.set()
        assert release_warmup.wait(10), "test never released warm-up"
        log.append("warmup:end")

    scheduler = MarketScheduler(audit=None)
    scheduler.register_scanner_warmup(warmup)
    scheduler.register_market_open(lambda: log.append("market_open"))
    missed: list[str] = []
    scheduler._scheduler.add_listener(lambda ev: missed.append(ev.job_id), EVENT_JOB_MISSED)

    scheduler.start(paused=True)
    scheduler._scheduler.get_job("market_open").modify(misfire_grace_time=1)
    scheduler.resume()
    try:
        # main()'s replay: warm-up runs synchronously and is slow.
        replay = threading.Thread(target=scheduler.run_sync, args=("scanner_warmup",))
        replay.start()
        assert warmup_started.wait(5)

        # 09:15 arrives while warm-up is still running.
        scheduler.run_now("market_open")
        real_time.sleep(2.0)  # > grace: a job left waiting in the queue would now be dropped
        assert "market_open" not in log, f"market_open ran while warm-up was in progress: {log}"

        release_warmup.set()
        replay.join(5)
        deadline = real_time.monotonic() + 5
        while "market_open" not in log and real_time.monotonic() < deadline:
            real_time.sleep(0.05)
    finally:
        release_warmup.set()  # never leave the warm-up thread blocked on a failed assertion
        scheduler.stop(wait=True)

    assert log == ["warmup:start", "warmup:end", "market_open"]
    assert missed == [], f"market_open was dropped as a misfire: {missed}"


def test_restart_at_0910_with_warmup_running_past_0915_opens_once_after_warmup(
    monkeypatch, tmp_path
):
    """
    Restart at 09:10; the replayed warm-up's backfill runs until 09:17. The
    scheduled market_open comes due mid-warm-up: it must run once, only
    after warm-up has completed, and the replay must be one atomic batch so
    nothing scheduled can slip in between pre-market and warm-up either.
    """
    h = boot_main_at(monkeypatch, tmp_path, at(9, 10), warmup_runs_until=at(9, 17))

    assert h.clock.now(IST) == at(9, 17)  # time passed inside warm-up
    assert h.scheduler.errors == []
    assert h.scheduler.sync_batches == [["pre_market_boot", "scanner_warmup"]], (
        f"replay must be a single atomic batch; got {h.scheduler.sync_batches}"
    )

    ev = h.events
    assert ev.index("scheduler.done:scanner_warmup") < ev.index("scheduler.fire:market_open"), (
        f"market_open started before warm-up finished: {ev}"
    )
    assert morning_phases(ev) == EXPECTED_MORNING_SEQUENCE
    assert ev.count("scheduler.fire:market_open") == 1
    assert h.ws.started and h.tick_writer.started and h.candle_aggregator.started

    h.scheduler.run_until(at(23, 59))
    assert h.scheduler.errors == []
    assert phase_counts(h.events) == {phase: 1 for phase in EXPECTED_MORNING_SEQUENCE}


# ── Restarts at / after square-off ───────────────────────────
#
# 15:15 square-off → 15:30 close. A restart in between must keep the feed
# and candle collection running until close, but must not open any new
# trade. Position state is in-memory only, so positions opened before the
# restart are unknown: that must be called out, not reported as closed.


def _unknown_positions_alerts(h: Harness) -> list[tuple[str, str, str]]:
    return [
        a for a in h.alerts
        if a[0] == "risk" and "not tracked" in a[2].lower() and "not squared off" in a[2].lower()
    ]


def test_restart_at_1516_keeps_feed_until_close_but_opens_no_new_trades(monkeypatch, tmp_path):
    h = boot_main_at(monkeypatch, tmp_path, at(15, 16))
    assert h.clock.now(IST) == at(15, 16)
    assert h.scheduler.errors == []

    # Feed and candle collection are up, and square-off state is re-established, in order.
    assert h.scheduler.sync_batches == [
        ["pre_market_boot", "scanner_warmup", "market_open", "intraday_squareoff"]
    ], f"replay batch: {h.scheduler.sync_batches}"
    assert morning_phases(h.events) == EXPECTED_MORNING_SEQUENCE
    assert h.ws.started and h.tick_writer.started and h.candle_aggregator.started
    assert sorted(h.ws.subscribed) == sorted(WATCHLIST_TOKENS)

    # A tick arriving the instant the socket starts — i.e. *during* the replay,
    # before the square-off job has run — must not be able to open a trade.
    first = h.order_at_feed_start
    assert first is not None, "no trade was attempted at feed start"
    assert not first.success, (
        f"BUY filled on the first tick after restart, before square-off was replayed: "
        f"{first.message}; events: {h.events}"
    )
    assert "entries closed" in first.message.lower(), f"wrong reason: {first.message}"

    # Once start-up has finished it is still closed — and not merely because
    # the market-hours gate thinks the market is closed (it is 15:16).
    result = h.try_open_long()
    assert result is not None and not result.success, f"BUY entry went through: {result}"
    assert "entries closed" in result.message.lower(), f"wrong reason: {result.message}"

    # A SELL signal with nothing held is a short entry, not an exit: blocked
    # at the risk gate, not merely by the paper trader having no position.
    sell = h.try_sell(symbol="TCS", token="11536")
    assert sell is not None and not sell.success
    assert "entries closed" in sell.message.lower(), (
        f"SELL with no position passed the risk gate: {sell.message}"
    )

    # Positions opened before the restart are unknown: say so, don't report them closed.
    assert _unknown_positions_alerts(h), f"no pre-restart positions warning; alerts: {h.alerts}"

    # The session then ends normally: close stops the feed, setup is not repeated.
    fired = h.scheduler.run_until(at(23, 59))
    assert h.scheduler.errors == []
    assert "market_close" in fired and h.ws.stopped
    assert phase_counts(h.events) == {phase: 1 for phase in EXPECTED_MORNING_SEQUENCE}
    assert h.events.count("scheduler.fire:intraday_squareoff") == 1


def test_restart_at_11_squareoff_warns_about_untracked_pre_restart_positions(restarted_at_11):
    """Even a mid-morning restart cannot see positions opened by the previous process."""
    h = restarted_at_11
    h.scheduler.run_until(at(15, 20))
    assert h.events.count("scheduler.fire:intraday_squareoff") == 1
    assert _unknown_positions_alerts(h), f"alerts: {h.alerts}"
    result = h.try_open_long()
    assert result is not None and not result.success and "square" in result.message.lower()


@pytest.mark.parametrize("restart_time", [at(15, 30, 0), at(16, 30)], ids=["15:30:00", "16:30"])
def test_restart_at_or_after_close_starts_no_feed_and_opens_no_trades(
    monkeypatch, tmp_path, restart_time
):
    h = boot_main_at(monkeypatch, tmp_path, restart_time)
    assert h.clock.now(IST) == restart_time
    assert h.scheduler.errors == []

    # Market is closed: no morning replay, no feed, nothing subscribed for live.
    assert h.scheduler.sync_batches == []
    assert not h.ws.started and not h.tick_writer.started and not h.candle_aggregator.started

    # The after-close catch-up still prepares data for post-market (also at 15:30:00 sharp).
    assert "backfill_all_intervals" in h.ingestion.calls
    assert "compute_and_store_indicators" in h.ingestion.calls
    assert "load_warmup_data" in h.ingestion.calls

    # No new trade can be opened.
    result = h.try_open_long()
    assert result is not None and not result.success, f"BUY entry went through: {result}"

    # Rest of the day runs without errors and never starts the feed.
    h.scheduler.run_until(restart_time.replace(hour=23, minute=59))
    assert h.scheduler.errors == []
    assert not h.ws.started


# ── Can a SELL add exposure once entries are closed? ─────────


def _risk_manager_at(when: datetime, monkeypatch):
    import trading.risk_manager as risk_manager_module
    from trading.risk_manager import RiskConfig, RiskManager

    clock = FakeClock(when)
    monkeypatch.setattr(risk_manager_module, "datetime", make_frozen_datetime(clock))
    return RiskManager(config=RiskConfig(total_capital=1e7)), clock


def test_entries_closed_blocks_sells_that_would_open_or_flip_to_short(monkeypatch):
    """
    After square-off only orders that *reduce* an existing position may pass.
    A SELL with nothing held is a short entry (in LIVE mode the broker would
    fill it as MIS short); a SELL larger than the holding would flip short.
    """
    risk, clock = _risk_manager_at(at(15, 16), monkeypatch)
    risk.close_new_entries("intraday square-off done")
    held = [make_position(symbol="RELIANCE", quantity=10)]

    def sell(symbol, qty, positions):
        return risk.evaluate(
            make_order_request(symbol=symbol, transaction_type=TransactionType.SELL, quantity=qty),
            positions,
        )

    assert not risk.evaluate(make_order_request(transaction_type=TransactionType.BUY), held).approved

    exit_full = sell("RELIANCE", 10, held)
    assert exit_full.approved, exit_full.reason          # genuine exit
    exit_part = sell("RELIANCE", 4, held)
    assert exit_part.approved, exit_part.reason          # partial exit

    short_entry = sell("TCS", 5, held)                   # nothing held in TCS
    assert not short_entry.approved, "SELL with no position must not pass after square-off"
    assert "entries closed" in short_entry.reason.lower()

    flip = sell("RELIANCE", 25, held)                    # more than held
    assert not flip.approved, "SELL beyond the holding would flip to short"
    assert "entries closed" in flip.reason.lower()

    # A same-day reset (pre-market replayed after a restart) must NOT reopen
    # entries that square-off closed — that is what let the first tick trade.
    risk.reset_daily()
    assert risk.entries_closed
    assert not sell("TCS", 5, held).approved

    # The next trading day's pre-market reset does reopen them (checked
    # during the next session so the market-hours gate is not what decides).
    clock.set(at(8, 30) + timedelta(days=1))
    risk.reset_daily()
    assert not risk.entries_closed
    clock.set(at(10, 0) + timedelta(days=1))
    assert sell("TCS", 5, held).approved


# ── Set-up failures during the restart replay ────────────────
#
# If pre-market fails, or warm-up cannot subscribe or load its data, the
# feed must NOT be started: a half-prepared session (no tokens, no caches,
# no router) would scan nothing useful and collect candles for nothing.


REQUIRED_STEP_FAILURES = [
    ("auth", "pre_market_boot"),
    ("pre_market", "pre_market_boot"),
    ("warmup_subscribe", "scanner_warmup"),
    ("warmup_data", "scanner_warmup"),
]


@pytest.mark.parametrize("fail, failing_job", REQUIRED_STEP_FAILURES)
def test_restart_at_11_does_not_open_market_when_setup_fails(monkeypatch, tmp_path, fail, failing_job):
    h = boot_main_at(monkeypatch, tmp_path, at(11, 0), fail=fail)
    target = SETUP_FAILURES[fail][1]
    assert f"{target}:raises" in h.events  # the failure really happened

    # The failing job was attempted, but market_open must not have run.
    assert f"scheduler.fire:{failing_job}" in h.events
    assert "market_open" not in morning_phases(h.events), (
        f"feed started after {failing_job} failed ({target}); phases: "
        f"{morning_phases(h.events)}; events: {h.events}"
    )
    assert not h.ws.started and not h.tick_writer.started and not h.candle_aggregator.started

    # The operator is told which step failed and that the session was not started.
    assert any(
        kind == "risk" and failing_job in message and "not started" in message.lower()
        for kind, _title, message in h.alerts
    ), f"no clear alert about the aborted start-up; alerts: {h.alerts}"

    # And nothing later in the day quietly starts it either.
    h.scheduler.run_until(at(23, 59))
    assert not h.ws.started


@pytest.mark.parametrize("fail, failing_job", REQUIRED_STEP_FAILURES)
def test_scheduled_market_open_refuses_when_setup_failed_earlier_today(
    monkeypatch, tmp_path, fail, failing_job
):
    """
    Normal day, no restart: the process is up at 08:00 and the scheduler
    runs pre-market (08:30), warm-up (09:00), market open (09:15). A
    required step failing must stop the dependent steps and keep the feed
    down, exactly as in the restart path.
    """
    h = boot_main_at(monkeypatch, tmp_path, at(8, 0), fail=fail)
    assert h.scheduler.sync_batches == []  # nothing to replay before pre-market

    fired = h.scheduler.run_until(at(9, 20))
    assert "market_open" in fired  # the cron slot came and went...
    assert "market_open" not in morning_phases(h.events), (
        f"feed started although {failing_job} failed; phases {morning_phases(h.events)}"
    )
    assert not h.ws.started and not h.tick_writer.started and not h.candle_aggregator.started

    if failing_job == "pre_market_boot":
        # Warm-up depends on pre-market: it must not subscribe or load data.
        assert "ws.subscribe" not in h.events and "ingestion.load_warmup_data" not in h.events

    assert any(
        kind == "risk" and "not started" in message.lower()
        for kind, _title, message in h.alerts
    ), f"no clear alert; alerts: {h.alerts}"

    h.scheduler.run_until(at(23, 59))
    assert not h.ws.started


@pytest.mark.parametrize("restart_time", [at(8, 0), at(11, 0)], ids=["scheduled-08:00", "restart-11:00"])
def test_backfill_failure_runs_degraded_session_with_entries_blocked(monkeypatch, tmp_path, restart_time):
    """
    Backfill failing is not fatal: scanning and candle collection can run on
    their own. But the trading data is unverified, so no new entries, and the
    session must not be reported as fully ready.
    """
    h = boot_main_at(monkeypatch, tmp_path, restart_time, fail="backfill")
    h.scheduler.run_until(max(restart_time, at(9, 20)))
    assert h.scheduler.errors == []
    assert "ingestion.backfill_all_intervals:raises" in h.events

    # Feed and candle collection are up, warm-up data was loaded.
    assert morning_phases(h.events) == EXPECTED_MORNING_SEQUENCE
    assert h.ws.started and h.tick_writer.started and h.candle_aggregator.started
    assert "ingestion.load_warmup_data" in h.events

    # But entries are blocked because the data is unverified...
    result = h.try_open_long()
    assert result is not None and not result.success, f"entry filled on unverified data: {result}"
    assert "unverified" in result.message.lower(), result.message

    # ...and the operator is told the session is degraded, not fully ready.
    assert any(
        kind == "risk" and "unverified" in (title + message).lower() for kind, title, message in h.alerts
    ), f"no degraded-session alert; alerts: {h.alerts}"
    assert not any(
        title == "Market Open" and "started" in message and "unverified" not in message.lower()
        for _k, title, message in h.alerts
    ), f"session reported fully ready: {h.alerts}"


def test_successful_rerun_of_warmup_releases_only_the_data_unverified_hold(monkeypatch, tmp_path):
    """
    Degraded at 11:00 (backfill failed). The operator fixes the historical
    API and re-runs warm-up: entries reopen. The same re-run after 15:15
    must NOT reopen entries, because the square-off hold is independent.
    """
    healthy_backfill = FakeIngestion.backfill_all_intervals  # before the failure is injected
    h = boot_main_at(monkeypatch, tmp_path, at(11, 0), fail="backfill")
    assert not h.try_open_long().success

    # Historical API is back; re-run warm-up manually.
    monkeypatch.setattr(FakeIngestion, "backfill_all_intervals", healthy_backfill)
    h.scheduler.run_now("scanner_warmup")
    assert h.scheduler.errors == []
    assert h.try_open_long().success, "data verified by the re-run — entries should be open again"

    # After square-off, a (successful) warm-up re-run must not reopen entries.
    h.scheduler.run_until(at(15, 20))
    assert "scheduler.fire:intraday_squareoff" in h.events
    h.scheduler.run_now("scanner_warmup")
    result = h.try_open_long()
    assert not result.success and "square-off" in result.message.lower(), result.message


# ── market_open itself: component order and rollback ─────────


def test_first_tick_reaches_writer_and_aggregator_only_after_they_are_started(monkeypatch, tmp_path):
    """
    The socket must be the last thing to start: a tick on the first second
    of the feed must find the tick writer and candle aggregator running.
    """
    h = boot_main_at(monkeypatch, tmp_path, at(11, 0))
    assert h.scheduler.errors == []
    assert h.ws.running and h.tick_writer.running and h.candle_aggregator.running

    writer_hits = [e for e in h.events if e.startswith("tick_writer.buffer(")]
    agg_hits = [e for e in h.events if e.startswith("candle_aggregator.on_tick(")]
    assert writer_hits and agg_hits, f"first tick never reached the persistence path: {h.events}"
    assert writer_hits == ["tick_writer.buffer(started=True)"], (
        f"tick writer received the first tick before it was started: {writer_hits}"
    )
    assert agg_hits == ["candle_aggregator.on_tick(started=True)"], (
        f"candle aggregator received the first tick before it was started: {agg_hits}"
    )


@pytest.mark.parametrize("component_failure", ["writer_start", "aggregator_start"])
def test_market_open_leaves_all_components_stopped_when_one_fails_to_start(
    monkeypatch, tmp_path, component_failure
):
    """
    Restart at 15:16 with a failed backfill, so both the square-off and the
    data-unverified holds are in place. One persistence component fails to
    start: the feed must not be left running half-persisted, the job fails
    with a clear alert, and the entry holds are untouched.
    """
    h = boot_main_at(monkeypatch, tmp_path, at(15, 16), fail=("backfill", component_failure))
    target = SETUP_FAILURES[component_failure][1]
    assert f"{target}:raises" in h.events

    # Safe state: nothing left running.
    assert not h.ws.running, "WebSocket left running after a persistence component failed to start"
    assert not h.tick_writer.running
    assert not h.candle_aggregator.running

    # The job failed (and so the batch stopped), with a clear alert.
    assert any(name == "market_open" for name, _e in h.scheduler.errors), h.scheduler.errors
    assert any(
        kind == "risk" and "market open" in title.lower() and "not" in message.lower()
        for kind, title, message in h.alerts
    ), f"no alert about the failed market open: {h.alerts}"

    # Entry holds intact: square-off (restart after 15:15) and data-unverified (backfill).
    holds = h.order_manager._risk_manager._entry_holds
    assert {"square_off", "data_unverified"} <= set(holds), holds
    result = h.try_open_long()
    assert result is not None and not result.success

    # Nothing later in the day restarts a half-open feed.
    h.scheduler.run_until(at(23, 59))
    assert not h.ws.running


# ── Spilled candle for a token no longer on the watchlist (real recovery path) ──


def test_restart_at_11_recovers_spilled_candle_for_unwatched_token_and_saves_it_once(monkeypatch, tmp_path):
    """
    The previous run spilled a 5-minute candle for token 9999 at shutdown.
    Since then 9999 was removed from every watchlist, so today's warm-up
    does not subscribe it. The real CandleAggregator must still recover the
    candle, get a stock_id for it (looked up alongside the subscribed
    tokens), write it exactly once, and clear the spill file.
    """
    from data.streams.candle_aggregator import CandleBucket

    spill = tmp_path / "data" / "candle_spill_5m.json"
    spill.parent.mkdir(parents=True)
    bucket = CandleBucket(
        token="9999", symbol="OLDCO", interval_start=at(15, 25) - timedelta(days=1),
        open=100.0, high=101.0, low=99.5, close=100.5, volume=1200, tick_count=8,
    )
    spill.write_text(json.dumps({"interval": "FIVE_MINUTE", "candles": [bucket.to_dict()]}))

    h = boot_main_at(monkeypatch, tmp_path, at(11, 0), real_candle_aggregator=True)
    assert h.scheduler.errors == []
    assert "9999" not in h.ws.subscribed  # genuinely unwatched today

    # Mapped (via the unsubscribed-token lookup) and saved exactly once.
    saved = [(iv, row) for iv, row in h.db.inserted_candles if row[0] == KNOWN_STOCK_IDS["9999"]]
    assert len(saved) == 1, f"expected one saved candle for 9999, got {saved}; all: {h.db.inserted_candles}"
    interval, row = saved[0]
    assert interval == "FIVE_MINUTE"
    assert row[1] == bucket.interval_start and row[6] == 1200
    assert h.candle_aggregator.pending_candles == 0
    assert not spill.exists(), "spill file must be cleared once its candle is written"

    # The aggregator started afterwards (which recovers again) and the rest
    # of the day — including the close-time flush — must not write it twice.
    assert h.candle_aggregator.running if hasattr(h.candle_aggregator, "running") else True
    h.scheduler.run_until(at(23, 59))
    assert h.scheduler.errors == []
    saved_after = [r for r in h.db.inserted_candles if r[1][0] == KNOWN_STOCK_IDS["9999"]]
    assert len(saved_after) == 1, f"candle for 9999 written more than once: {saved_after}"


# ── Rollback must also stop the component whose start() raised mid-way ──


@pytest.mark.parametrize("component_failure", ["writer_start_partial", "aggregator_start_partial"])
def test_market_open_rollback_stops_the_component_that_failed_after_starting(
    monkeypatch, tmp_path, component_failure
):
    """
    A start() that has already flagged itself running (thread spawned) and
    then raises must be stopped by the rollback too — not just the
    components that started cleanly before it.
    """
    h = boot_main_at(monkeypatch, tmp_path, at(15, 16), fail=("backfill", component_failure))
    label = SETUP_FAILURES[component_failure][1].split(".")[0]
    assert f"{label}.start:raises" in h.events
    failed_component = getattr(h, label)
    assert failed_component.started, "precondition: start() did its work before raising"

    assert not failed_component.running, f"{label} left running after its start() raised"
    assert not h.ws.running and not h.tick_writer.running and not h.candle_aggregator.running

    holds = h.order_manager._risk_manager._entry_holds
    assert {"square_off", "data_unverified"} <= set(holds), holds
    assert any(name == "market_open" for name, _e in h.scheduler.errors)
