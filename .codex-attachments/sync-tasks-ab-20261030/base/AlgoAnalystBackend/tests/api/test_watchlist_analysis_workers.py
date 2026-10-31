"""Isolation tests for the CPU-bound watchlist analysis scheduler."""

from datetime import datetime, timezone

from api import server


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _RecordingProcessPool:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.submissions = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def submit(self, function, *args):
        self.submissions.append((function, args))
        return _ImmediateFuture(function(*args))

    def shutdown(self, **_):
        return None


def test_watchlist_analysis_uses_bounded_spawned_process_workers(monkeypatch):
    _RecordingProcessPool.instances.clear()
    job_id = "job-1"
    monkeypatch.setattr(
        server,
        "_watchlist_analysis_jobs",
        {
            job_id: {
                "status": "queued",
                "completed_stocks": 0,
                "completed_backtests": 0,
                "successful_backtests": 0,
                "failed_stocks": 0,
                "total_backtests": 2,
            }
        },
    )
    monkeypatch.setattr(server, "_watchlist_analysis_executors", {})
    monkeypatch.setattr(server, "ProcessPoolExecutor", _RecordingProcessPool)
    monkeypatch.setattr(server, "as_completed", lambda futures: futures)
    monkeypatch.setattr(
        server,
        "_watchlist_analysis_stock",
        lambda symbol, *_: {
            "symbol": symbol,
            "stock_name": symbol,
            "status": "completed",
            "market_profile": {},
            "best_combination": None,
            "combinations": [{"status": "completed"}],
        },
    )
    monkeypatch.setattr(server, "_filter_leaderboard", lambda *_, **__: [])
    monkeypatch.setattr(server, "_watchlist_regime_recommendations", lambda _: [])

    request = server.WatchlistFilterAnalysisRequest(
        symbols=["ONE-EQ", "TWO-EQ"],
        start_date="2025-01-01",
        end_date="2025-12-31",
    )
    server._run_watchlist_analysis_job(
        job_id,
        request,
        ["ONE-EQ", "TWO-EQ"],
        ["oneil_growth_breakout"],
        {},
    )

    pool = _RecordingProcessPool.instances[0]
    assert pool.kwargs["max_workers"] == 2
    assert pool.kwargs["mp_context"].get_start_method() == "spawn"
    assert pool.kwargs["initializer"] is server._initialize_cpu_analysis_worker
    assert len(pool.submissions) == 2
    assert server._watchlist_analysis_jobs[job_id]["status"] == "completed"


def test_watchlist_analysis_splits_portfolio_capital_evenly_before_starting_workers(monkeypatch):
    captured = []

    class _RecordingThread:
        def __init__(self, *, target, args, **_):
            captured.append((target, args))

        def start(self):
            return None

    monkeypatch.setattr(server, "_watchlist_analysis_jobs", {})
    monkeypatch.setattr(server, "_watchlist_analysis_executors", {})
    monkeypatch.setattr(server, "Thread", _RecordingThread)
    monkeypatch.setattr(
        server,
        "_resolve_watchlist_analysis_symbols",
        lambda _: (["ONE-EQ", "TWO-EQ"], {"type": "manual", "name": "2 pasted symbols"}),
    )

    response = server.start_watchlist_filter_analysis(
        server.WatchlistFilterAnalysisRequest(
            symbols=["ONE-EQ", "TWO-EQ"],
            start_date="2025-01-01",
            end_date="2025-12-31",
            initial_capital=1_000_000,
        )
    )

    assert response["portfolio_initial_capital"] == 1_000_000
    assert response["capital_per_stock"] == 500_000
    assert captured[0][1][1].initial_capital == 500_000


def test_new_watchlist_request_cancels_the_active_analysis_and_starts_immediately(monkeypatch):
    captured = []

    class _RecordingThread:
        def __init__(self, *, target, args, **_):
            captured.append((target, args))

        def start(self):
            return None

    class _LiveProcess:
        terminated = False

        def is_alive(self):
            return True

        def terminate(self):
            self.terminated = True

    class _ActiveExecutor:
        def __init__(self):
            self.process = _LiveProcess()
            self._processes = {1: self.process}
            self.shutdown_args = None

        def shutdown(self, **kwargs):
            self.shutdown_args = kwargs

    active_executor = _ActiveExecutor()
    active_job_id = "active-job"
    monkeypatch.setattr(
        server,
        "_watchlist_analysis_jobs",
        {
            active_job_id: {
                "job_id": active_job_id,
                "status": "running",
                "created_at": datetime.now(timezone.utc),
            }
        },
    )
    monkeypatch.setattr(server, "_watchlist_analysis_executors", {active_job_id: active_executor})
    monkeypatch.setattr(server, "Thread", _RecordingThread)
    monkeypatch.setattr(
        server,
        "_resolve_watchlist_analysis_symbols",
        lambda _: (["NEW-EQ"], {"type": "manual", "name": "1 pasted symbol"}),
    )

    response = server.start_watchlist_filter_analysis(
        server.WatchlistFilterAnalysisRequest(
            symbols=["NEW-EQ"],
            start_date="2025-01-01",
            end_date="2025-12-31",
        )
    )

    cancelled_job = server._watchlist_analysis_jobs[active_job_id]
    assert cancelled_job["status"] == "cancelled"
    assert cancelled_job["error"] == "Superseded by a newer watchlist analysis request."
    assert active_executor.process.terminated is True
    assert active_executor.shutdown_args == {"wait": False, "cancel_futures": True}
    assert response["status"] == "queued"
    assert captured[0][1][0] == response["job_id"]


def test_analysis_worker_connection_skips_redundant_migrations(monkeypatch):
    created = []

    class FakeSettings:
        DB_HOST = "localhost"
        DB_PORT = 5432
        DB_NAME = "test"
        DB_USER = "test"
        DB_PASSWORD = "test"
        DB_MIN_CONNECTIONS = 1
        DB_MAX_CONNECTIONS = 1

    class FakeDatabase:
        def __init__(self, **_):
            self.connected = False
            self.migrations = 0
            created.append(self)

        def connect(self):
            self.connected = True

        def run_migrations(self):
            self.migrations += 1

    monkeypatch.setattr(server, "Settings", FakeSettings)
    monkeypatch.setattr(server, "Database", FakeDatabase)
    monkeypatch.setattr(server, "_db", None)
    monkeypatch.setattr(server, "_stores", {})
    monkeypatch.setattr(server, "_is_cpu_analysis_worker", True)
    for name in (
        "CandleStore",
        "CandleCoverageStore",
        "StockStore",
        "TechnicalIndicatorStore",
        "WatchlistStore",
        "WatchlistAlgorithmBindingStore",
        "ScannerSignalStore",
        "AppConfigStore",
        "FilterProfileStore",
        "TrendlineStore",
    ):
        monkeypatch.setattr(server, name, lambda database: database)

    connection = server.get_db()

    assert connection.connected is True
    assert created[0].migrations == 0
