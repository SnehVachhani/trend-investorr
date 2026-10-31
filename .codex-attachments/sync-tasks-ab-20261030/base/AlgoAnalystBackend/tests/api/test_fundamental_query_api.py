"""API orchestration checks for query-selected technical backtests."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from api import server
from engine.filter_engine.presets import get_filter_preset

IST = ZoneInfo("Asia/Kolkata")


def test_setup_catalog_is_served_from_the_backtest_registry():
    result = server.list_fundamental_setup_presets()

    assert [setup["id"] for setup in result["setups"]] == list(server.SETUPS)
    assert all(setup["entry_rule"] and setup["exit_rule"] for setup in result["setups"])
    assert all(setup["candle_interval"] == "5m" for setup in result["setups"])
    assert all(get_filter_preset(setup_id).timeframe.value == "5m" for setup_id in server.FILTER_BACKED_SETUP_IDS)


def test_setup_candles_keep_every_five_minute_bar_and_deduplicate_exact_timestamps():
    class FakeDatabase:
        def fetch_all(self, sql, params):
            assert "candles_5m" in sql
            assert params[0] == 7
            return [
                {"timestamp": datetime(2026, 9, 18, 9, 15, tzinfo=IST), "open": 99, "high": 101, "low": 98, "close": 100, "volume": 500},
                {"timestamp": datetime(2026, 9, 18, 9, 20, tzinfo=IST), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 600},
                {"timestamp": datetime(2026, 9, 18, 9, 20, tzinfo=IST), "open": 101, "high": 107, "low": 100, "close": 106, "volume": 1200},
                {"timestamp": datetime(2026, 9, 18, 9, 25, tzinfo=IST), "open": 106, "high": 108, "low": 105, "close": 107, "volume": 900},
                {"timestamp": datetime(2026, 9, 18, 9, 30, tzinfo=IST), "open": 107, "high": 107, "low": 107, "close": 107, "volume": -20},
            ]

    quality_counts = {}
    rows = server._load_fundamental_setup_candles(
        FakeDatabase(), 7, date(2026, 9, 18), date(2026, 9, 21),
        quality_counts=quality_counts,
    )
    assert [row["timestamp"].time() for row in rows] == [time(9, 15), time(9, 20), time(9, 25)]
    assert [row["close"] for row in rows] == [100, 106, 107]
    assert quality_counts == {"5m": 1}


def test_filter_backtest_inputs_skip_malformed_ohlcv_and_count_them(monkeypatch):
    valid = {
        "timestamp": datetime(2026, 9, 18, 9, 15, tzinfo=IST),
        "open": 100, "high": 102, "low": 99, "close": 101, "volume": 50,
    }
    invalid = {
        "timestamp": datetime(2026, 9, 18, 9, 20, tzinfo=IST),
        "open": 100, "high": 99, "low": 98, "close": 101, "volume": 50,
    }

    class FakeDatabase:
        def fetch_all(self, sql, params):
            assert "candles_5m" in sql
            assert params[0] == 7
            return [valid, invalid]

    monkeypatch.setattr(server, "get_db", lambda: FakeDatabase())
    monkeypatch.setattr(
        server,
        "_resolve_stock_id",
        lambda _db, _symbol: {"id": 7, "symbol": "ALPHA-EQ", "name": "Alpha"},
    )
    quality_counts = {}
    request = server.FilterBacktestRequest(
        symbol="ALPHA-EQ", start_date="2026-09-18", end_date="2026-09-18",
    )

    shared = server._load_filter_backtest_inputs(
        request,
        {"5m": {}},
        execution_timeframe=server.Timeframe.FIVE_MINUTE,
        quality_counts=quality_counts,
    )

    points = shared[4][server.Timeframe.FIVE_MINUTE]
    assert [point.timestamp for point in points] == [valid["timestamp"]]
    assert quality_counts == {"5m": 1}


def test_fundamental_backtests_share_bounded_spawn_pool(monkeypatch):
    created = []

    class FakeProcessPool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setattr(server, "_fundamental_backtest_executor", None)
    monkeypatch.setattr(server, "ProcessPoolExecutor", FakeProcessPool)

    first = server._get_fundamental_backtest_executor()
    second = server._get_fundamental_backtest_executor()

    assert first is second
    assert len(created) == 1
    assert first.kwargs["max_workers"] == server._MAX_WATCHLIST_ANALYSIS_WORKERS
    assert first.kwargs["initializer"] is server._initialize_cpu_analysis_worker
    assert first.kwargs["mp_context"].get_start_method() == "spawn"


def test_create_update_and_run_capture_immutable_query_version(monkeypatch):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    original = [{"metric": "profit_growth_1y_pct", "operator": ">=", "value": 10}]
    revised = [{"metric": "roe_pct", "operator": "gte", "value": 15}]

    class FakeDatabase:
        row = None
        inserts = []

        def execute_returning(self, sql, params):
            if sql.startswith("INSERT INTO fundamental_queries"):
                self.row = {
                    "id": 7, "name": params[0], "version": 1,
                    "rules": json.loads(params[1]), "created_at": now, "updated_at": now,
                }
            else:
                assert sql.startswith("UPDATE fundamental_queries SET")
                assert params[2] == 7
                self.row = {
                    **self.row,
                    "name": params[0], "version": self.row["version"] + 1,
                    "rules": json.loads(params[1]), "updated_at": now,
                }
            return dict(self.row)

        def fetch_one(self, sql, params):
            assert "FROM fundamental_queries" in sql
            assert params == (7,)
            return dict(self.row)

        def execute(self, sql, params):
            self.inserts.append((sql, params))
            return 1

    class NoStartThread:
        calls = []

        def __init__(self, *, target, args, **_):
            self.calls.append((target, args))

        def start(self):
            pass

    db = FakeDatabase()
    monkeypatch.setattr(server, "get_db", lambda: db)
    monkeypatch.setattr(server, "Thread", NoStartThread)
    monkeypatch.setattr(server, "_fundamental_backtest_jobs", {})
    first = server.create_fundamental_query(server.FundamentalQueryRequest(name="Growth", rules=original))["query"]
    second = server.update_fundamental_query(
        7, server.FundamentalQueryRequest(name="Profitable growth", rules=revised)
    )["query"]
    assert first["version"] == 1
    assert first["rules"] == original
    assert second["version"] == 2
    assert second["rules"] == [{"metric": "roe_pct", "operator": ">=", "value": 15.0}]
    assert "return on equity" in second["query_text"]

    setup_id = next(iter(server.SETUPS))
    job = server.start_fundamental_query_backtest(server.FundamentalQueryBacktestRequest(
        query_id=7, start_date=date(2025, 1, 1), end_date=date(2025, 6, 30),
        setup_ids=[setup_id],
    ))
    insert_sql, insert_params = db.inserts[-1]
    assert "INSERT INTO fundamental_query_backtest_runs" in insert_sql
    captured = json.loads(insert_params[2])
    assert captured["version"] == 2
    assert captured["rules"] == second["rules"]
    assert NoStartThread.calls[0][1][2] == captured
    assert job["query"]["version"] == 2

    # Editing the saved screen after dispatch does not mutate the running
    # task's query snapshot or its persisted historical explanation.
    server.update_fundamental_query(7, server.FundamentalQueryRequest(name="Changed later", rules=original))
    assert db.row["version"] == 3
    assert job["query"]["version"] == 2
    assert json.loads(db.inserts[-1][1][2])["version"] == 2


def test_backtest_uses_only_previewed_stocks_and_never_rechecks_fundamentals(monkeypatch):
    job_id = "job-fixed-universe"
    snapshot_rules = [{"metric": "roe_pct", "operator": ">=", "value": 15.0}]
    query_snapshot = {"id": 7, "name": "Saved query", "version": 2, "rules": snapshot_rules}
    stock_a = {"stock_id": 1, "symbol": "ALPHA-EQ", "name": "Alpha"}
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "_fundamental_backtest_jobs", {
        job_id: {"job_id": job_id, "status": "queued", "created_at": now, "started_at": None},
    })

    class FakeDatabase:
        writes = []

        def fetch_all(self, sql, params):
            assert "FROM stocks" in sql
            assert params == ([1],)
            return [stock_a]

        def execute(self, sql, params):
            self.writes.append((sql, params))
            return 1

    db = FakeDatabase()
    monkeypatch.setattr(server, "get_db", lambda: db)
    monkeypatch.setattr(server, "store", lambda _name: object())
    monkeypatch.setattr(server, "config_from_store", lambda _store: object())

    def preview(_db, rules, as_of):
        raise AssertionError("the technical backtest must not re-run its stock query")

    monkeypatch.setattr(server, "evaluate_universe", preview)
    timestamps = [
        datetime(2024, 12, 31, 9, 15, tzinfo=IST),
        datetime(2024, 12, 31, 9, 20, tzinfo=IST),
        datetime(2025, 1, 2, 9, 15, tzinfo=IST),
        datetime(2025, 1, 2, 9, 20, tzinfo=IST),
        datetime(2025, 1, 2, 9, 25, tzinfo=IST),
    ]
    bars = [
        {"timestamp": timestamp, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        for timestamp in timestamps
    ]
    def load_candles(*_, quality_counts=None):
        if quality_counts is not None:
            quality_counts["5m"] = 2
        return bars

    monkeypatch.setattr(server, "_load_fundamental_setup_candles", load_candles)
    calls = []

    class ImmediateFuture:
        def __init__(self, function, args):
            self.value = function(*args)

        def result(self):
            return self.value

    class ImmediateProcessPool:
        submissions = []

        def submit(self, function, *args):
            self.submissions.append((function, args))
            return ImmediateFuture(function, args)

    process_pool = ImmediateProcessPool()
    monkeypatch.setattr(server, "_get_fundamental_backtest_executor", lambda: process_pool)
    monkeypatch.setattr(server, "as_completed", lambda futures: futures)

    def run_setup(**kwargs):
        calls.append(kwargs)
        return {
            "metrics": {"total_return_pct": 0.0, "max_drawdown_pct": 0.0},
            "trades": [],
        }

    monkeypatch.setattr(server, "run_setup", run_setup)
    setup_id = next(iter(server.SETUPS))
    server._run_fundamental_query_backtest_job(
        job_id,
        server.FundamentalQueryBacktestRequest(
            query_id=7, start_date=date(2025, 1, 1), end_date=date(2025, 6, 30),
            selected_stock_ids=[1], universe_as_of=date(2026, 9, 24),
            setup_ids=[setup_id], target_r=3.0, max_hold_bars=25, stop_buffer_pct=0.75,
        ),
        query_snapshot,
        [setup_id],
    )
    assert len(calls) == 1
    assert len(process_pool.submissions) == 1
    assert process_pool.submissions[0][0] is server._run_fundamental_setup_stock
    assert all("eligible" not in call for call in calls)
    assert all(call["target_r"] == 3.0 for call in calls)
    assert all(call["max_hold_bars"] == 25 for call in calls)
    assert all(call["stop_buffer_pct"] == 0.75 for call in calls)
    assert server._fundamental_backtest_jobs[job_id]["status"] == "completed"
    assert server._fundamental_backtest_jobs[job_id]["qualification_timeline"] == []
    assert server._fundamental_backtest_jobs[job_id]["universe_selection"] == {
        "as_of": "2026-09-24", "stock_count": 1,
        "symbols": ["ALPHA-EQ"], "source": "query_preview",
    }
    assert any(
        "Skipped 2 malformed OHLCV candles across 1 stocks" in note
        for note in server._fundamental_backtest_jobs[job_id]["limitations"]
    )
    assert "UPDATE fundamental_query_backtest_runs SET status = %s" in db.writes[-1][0]
    assert db.writes[-1][1][0] == "completed"
