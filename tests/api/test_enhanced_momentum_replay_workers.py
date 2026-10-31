"""Focused coverage for Enhanced Momentum replay progress and parallel batches."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from freezegun import freeze_time

from api import server


class _ImmediateFuture:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


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
        future = _ImmediateFuture(function(*args))
        self.submissions.append((function, args, future))
        return future


def test_refresh_schedule_uses_calendar_intervals_and_next_available_session():
    sessions = [
        date(2026, 1, 2),
        date(2026, 1, 16),
        date(2026, 2, 2),
        date(2026, 2, 27),
        date(2026, 3, 2),
        date(2026, 3, 31),
    ]

    assert server._enhanced_momentum_refresh_dates(
        sessions,
        date(2026, 1, 1),
        date(2026, 3, 31),
        "15_days",
    ) == [date(2026, 1, 2), date(2026, 1, 16), date(2026, 2, 2), date(2026, 2, 27), date(2026, 3, 2), date(2026, 3, 31)]
    assert server._enhanced_momentum_refresh_dates(
        sessions,
        date(2026, 1, 31),
        date(2026, 3, 31),
        "monthly",
    ) == [date(2026, 2, 2), date(2026, 3, 2), date(2026, 3, 31)]


def test_shared_capital_slots_carry_open_trades_and_fund_later_cohort_entries():
    candidates = {
        1: [{"stock_id": 1, "symbol": "OLD", "name": "Old Co", "rank": 1, "as_of_date": "2026-01-02"}],
        2: [{"stock_id": 2, "symbol": "NEW", "name": "New Co", "rank": 1, "as_of_date": "2026-01-05"}],
    }
    results = [
        {
            "stock_id": 1,
            "symbol": "OLD",
            "status": "completed",
            "trades": [{
                "signal_date": "2026-01-02",
                "entry_date": "2026-01-02",
                "entry_price": 50.0,
                "exit_date": "2026-01-05",
                "exit_price": 75.0,
                "target_price": 75.0,
                "target_hit": True,
                "stop_hit": False,
            }],
        },
        {
            "stock_id": 2,
            "symbol": "NEW",
            "status": "completed",
            "trades": [
                {
                    "signal_date": "2026-01-05",
                    "entry_date": "2026-01-03",
                    "entry_price": 50.0,
                    "exit_date": "2026-01-04",
                    "exit_price": 60.0,
                    "target_price": 60.0,
                    "target_hit": True,
                    "stop_hit": False,
                },
                {
                    "signal_date": "2026-01-05",
                    "entry_date": "2026-01-06",
                    "entry_price": 50.0,
                    "exit_date": "2026-01-07",
                    "exit_price": 60.0,
                    "target_price": 60.0,
                    "target_hit": True,
                    "stop_hit": False,
                },
            ],
        },
    ]

    stocks, summary = server._enhanced_backtest_portfolio_replay(
        results,
        candidates,
        {(1, "2026-01-02"): 1, (2, "2026-01-05"): 1},
        slot_count=1,
        capital_per_slot=100.0,
        charge_config=server.DeliveryChargeConfig(enabled=False),
        end_date=date(2026, 1, 7),
    )

    assert summary["starting_capital"] == 100.0
    assert summary["final_capital"] == 180.0
    assert summary["total_return_pct"] == 80.0
    assert summary["maximum_concurrent_positions"] == 1
    assert summary["blocked_for_full_capital"] == 1
    assert summary["total_trades"] == 2
    new_stock = next(stock for stock in stocks if stock["stock_id"] == 2)
    assert new_stock["trades"][0]["quantity"] == 3
    assert new_stock["trades"][0]["capital_slot"] == 1


def test_buy_and_hold_batch_uses_next_refresh_close_as_its_exit():
    snapshot_date = date(2026, 1, 2)
    refresh_date = date(2026, 2, 2)
    candidate = {"stock_id": 7, "symbol": "ROTATE", "name": "Rotate Co", "rank": 1}
    candles = [
        {"timestamp": datetime(2026, 1, 2, 15, 30, tzinfo=server.IST), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        {"timestamp": datetime(2026, 1, 5, 15, 30, tzinfo=server.IST), "open": 101.0, "high": 105.0, "low": 100.0, "close": 104.0},
        {"timestamp": datetime(2026, 2, 2, 15, 30, tzinfo=server.IST), "open": 110.0, "high": 113.0, "low": 109.0, "close": 112.0},
        {"timestamp": datetime(2026, 2, 3, 15, 30, tzinfo=server.IST), "open": 120.0, "high": 123.0, "low": 119.0, "close": 122.0},
    ]

    stock = server._enhanced_backtest_buy_hold_stock(
        candidate,
        candles,
        snapshot_date,
        refresh_date,
        1_000_000_000,
        server.DeliveryChargeConfig(enabled=False),
    )

    assert stock["trades"][0]["entry_date"] == "2026-01-05"
    assert stock["trades"][0]["exit_date"] == refresh_date.isoformat()
    assert stock["trades"][0]["exit_price"] == 112.0


def test_historical_enhanced_scan_uses_exchange_date_and_can_return_all_matches(monkeypatch):
    as_of = date(2026, 6, 29)
    seen_boundaries = []
    latest_timestamp = datetime(2026, 6, 29, 15, 30, tzinfo=server.IST)

    def scan_rows(_db, start, end):
        seen_boundaries.append((start, end))
        return [{"universe_count": 2}]

    def enriched_candidates(_rows, _market_context, *, min_history):
        assert min_history == 260
        return [
            {
                "stock_id": stock_id,
                "symbol": f"STOCK{stock_id}",
                "name": f"Stock {stock_id}",
                "industry": "Industrials",
                "market_cap_cr": 1000,
                "latest_timestamp": latest_timestamp,
                "latest_close": 100,
                "return_20d": 10,
                "return_60d": 20,
                "return_120d": 25,
                "return_252d": 30,
                "sma_20": 100,
                "sma_50": 90,
                "avg_turnover_20d_cr": 5,
                "volume_ratio": 2,
                "enhanced_score": 80,
                "observations": 260,
            }
            for stock_id in (1, 2)
        ]

    monkeypatch.setattr(server, "get_db", lambda: object())
    monkeypatch.setattr(server, "_enhanced_scan_rows", scan_rows)
    monkeypatch.setattr(server, "_enhanced_benchmark_candidates", lambda _db: [None, None])
    monkeypatch.setattr(server, "_load_enhanced_daily_candles", lambda *_args: [])
    monkeypatch.setattr(server, "build_market_context", lambda *_args: {"regime": "neutral"})
    monkeypatch.setattr(server, "enrich_candidates", enriched_candidates)

    response = server._scan_enhanced_momentum_universe(
        server.EnhancedMomentumScanRequest(as_of_date=as_of, max_results=1),
        include_details=False,
        include_all_results=True,
    )

    assert len(seen_boundaries) == 1
    start, end = seen_boundaries[0]
    assert start.tzinfo == server.IST
    assert end.tzinfo == server.IST
    assert end == datetime(2026, 6, 30, 0, 0, tzinfo=server.IST)
    assert response["matching_stocks"] == 2
    assert response["returned_stocks"] == 2
    assert {row["as_of_date"] for row in response["results"]} == {as_of.isoformat()}


def test_live_enhanced_scan_applies_optional_top_n_after_ranking(monkeypatch):
    as_of = date(2026, 6, 29)
    latest_timestamp = datetime(2026, 6, 29, 15, 30, tzinfo=server.IST)

    monkeypatch.setattr(server, "get_db", lambda: object())
    monkeypatch.setattr(server, "_enhanced_scan_rows", lambda *_args: [{"universe_count": 101}])
    monkeypatch.setattr(server, "_enhanced_benchmark_candidates", lambda _db: [None, None])
    monkeypatch.setattr(server, "_load_enhanced_daily_candles", lambda *_args: [])
    monkeypatch.setattr(server, "build_market_context", lambda *_args: {"regime": "neutral"})
    monkeypatch.setattr(server, "enrich_candidates", lambda _rows, _context, **_kwargs: [
        {
            "stock_id": stock_id,
            "symbol": f"STOCK{stock_id}",
            "name": f"Stock {stock_id}",
            "industry": "Industrials",
            "market_cap_cr": 1000,
            "latest_timestamp": latest_timestamp,
            "latest_close": 100,
            "return_20d": 10,
            "return_60d": 20,
            "avg_turnover_20d_cr": 5,
            "volume_ratio": 2,
            "enhanced_score": 100 - stock_id / 100,
            "observations": 260,
        }
        for stock_id in range(1, 102)
    ])

    def scan(top_n=None):
        return server._scan_enhanced_momentum_universe(
            server.EnhancedMomentumScanRequest(
                as_of_date=as_of,
                top_n=top_n,
                min_price=0,
                min_avg_turnover_cr=0,
                min_score=0,
                min_volume_ratio=0,
                min_history=60,
                trend_mode="any",
            ),
            include_details=False,
        )

    unbounded_response = scan()
    assert unbounded_response["matching_stocks"] == 101
    assert unbounded_response["returned_stocks"] == 101

    top_one_response = scan(1)
    assert top_one_response["matching_stocks"] == 101
    assert top_one_response["returned_stocks"] == 1
    assert [row["stock_id"] for row in top_one_response["results"]] == [1]


def test_top_n_buy_hold_replay_uses_explicit_dates_and_saves_one_strategy(monkeypatch):
    from_date = date(2026, 6, 26)
    snapshot_date = date(2026, 6, 29)
    end_date = date(2026, 6, 30)
    scan_dates = []

    class _Database:
        def fetch_all(self, sql, params):
            if "SELECT DISTINCT" in sql:
                return [{"session_date": snapshot_date}, {"session_date": end_date}]
            selected_ids = params[0]
            return [
                {
                    "stock_id": stock_id,
                    "timestamp": datetime(2026, 6, 30, 15, 30, tzinfo=server.IST),
                    "open": 100.0,
                    "high": 111.0,
                    "low": 99.0,
                    "close": 110.0,
                }
                for stock_id in selected_ids
            ]

        def execute_returning(self, _sql, _params):
            return {"id": 88, "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc)}

    candidates = [
        {
            "stock_id": stock_id,
            "symbol": f"STOCK{stock_id}",
            "name": f"Stock {stock_id}",
            "company_name": f"Stock {stock_id} Ltd",
            "industry": "Industrials",
            "as_of_date": snapshot_date.isoformat(),
            "rank": stock_id,
            "enhanced_score": 100.0 - stock_id,
        }
        for stock_id in range(1, 61)
    ]

    def scan_session(session_date, _filters):
        scan_dates.append(session_date)
        return {
            "matching_stocks": len(candidates),
            "returned_stocks": len(candidates),
            "market_context": {"regime": "neutral"},
            "formula": {},
            "results": candidates,
        }

    monkeypatch.setattr(server, "get_db", lambda: _Database())
    monkeypatch.setattr(server, "store", lambda _name: object())
    monkeypatch.setattr(server, "config_from_store", lambda _config: server.DeliveryChargeConfig(enabled=False))
    monkeypatch.setattr(server, "_enhanced_momentum_scan_session_worker", scan_session)

    response = server._execute_enhanced_momentum_buy_hold_backtest(
        server.EnhancedMomentumBacktestRequest(
            start_date=from_date,
            end_date=end_date,
            mode="buy_and_hold",
            top_n=30,
            capital_per_stock=100_000,
            filters=server.EnhancedMomentumScanRequest(max_results=1),
        )
    )

    run = response["run"]
    assert scan_dates == [snapshot_date]
    assert run["signal_cutoff_date"] == from_date.isoformat()
    assert run["end_date"] == end_date.isoformat()
    assert run["scan"]["strategy_mode"] == "buy_and_hold"
    assert run["scan"]["snapshot_matching_stocks"] == 60
    assert run["scan"]["selected_top_n"] == 30
    assert run["summary"]["returned_stocks"] == 30
    assert len(run["strategies"]) == 1
    strategy = run["strategies"][0]
    assert strategy["strategy_type"] == "buy_and_hold"
    assert strategy["summary"]["total_trades"] == 30
    assert strategy["stocks"][0]["trades"][0]["entry_date"] == end_date.isoformat()
    assert strategy["stocks"][0]["trades"][0]["entry_price"] == 100.0
    assert strategy["stocks"][0]["trades"][0]["exit_price"] == 110.0
    assert strategy["stocks"][0]["trades"][0]["target_price"] is None
    assert strategy["stocks"][0]["trades"][0]["stop_price"] is None


def test_replay_refreshes_ranked_cohorts_and_runs_setup_filters_on_active_members(monkeypatch):
    _RecordingProcessPool.instances.clear()
    sessions = [date(2026, 6, 2), date(2026, 6, 3)]
    candidate = {
        "stock_id": 101,
        "symbol": "ALPHA",
        "name": "Alpha Ltd",
        "company_name": "Alpha Ltd",
        "industry": "Industrials",
        "as_of_date": sessions[0].isoformat(),
        "rank": 1,
        "enhanced_score": 80.0,
    }
    second_candidate = {
        **candidate,
        "stock_id": 303,
        "symbol": "BETA",
        "name": "Beta Ltd",
        "rank": 2,
        "enhanced_score": 70.0,
    }
    stale_candidate = {**candidate, "stock_id": 202, "symbol": "STALE", "as_of_date": "2026-05-29"}

    class _Database:
        def fetch_all(self, sql, _params):
            if "SELECT DISTINCT" in sql:
                return [{"session_date": session} for session in sessions]
            assert "FROM candles_1d" in sql
            return []

        def execute_returning(self, _sql, _params):
            return {"id": 77, "created_at": datetime(2026, 6, 3, tzinfo=timezone.utc)}

    preset = SimpleNamespace(
        preset_id="mock_setup",
        name="Mock setup",
        description="Test setup",
        timeframe=server.Timeframe.FIVE_MINUTE,
        expression=SimpleNamespace(model_dump=lambda **_: {}),
        default_stop_loss_pct=2.0,
        default_take_profit_pct=5.0,
        max_hold_bars=20,
    )
    scans = []
    overlay_gates = []

    def scan_session(session_date, _filters):
        scans.append(session_date)
        return {
            "matching_stocks": 3 if session_date == sessions[0] else 0,
            "returned_stocks": 3 if session_date == sessions[0] else 0,
            "market_context": {"regime": session_date.isoformat()},
            "formula": {},
            "results": [candidate, stale_candidate, second_candidate] if session_date == sessions[0] else [],
        }

    def overlay_batch(stock, preset_ids, *_args):
        # _enhanced_backtest_selected_stock_batch(candidate, preset_ids, start,
        # end, capital, aggregate_requirements, requirements_by_preset,
        # charge_values, entry_signal_date_map, fundamental_setup_ids)
        *_, entry_signal_date_map, fundamental_setup_ids = _args
        assert fundamental_setup_ids == []
        overlay_gates.append(entry_signal_date_map)
        return {
            "results": {
                preset_id: {
                    "stock_id": stock["stock_id"],
                    "symbol": stock["symbol"],
                    "status": "completed",
                    "return_pct": 5.0,
                    "total_trades": 1,
                    "starting_capital": 100_000.0,
                    "final_capital": 105_000.0,
                    "trades": [{
                        "signal_date": sessions[0].isoformat(),
                        "entry_date": "2026-06-03T09:20:00+05:30",
                        "entry_price": 100.0,
                        "target_price": 105.0,
                        "stop_price": 98.0,
                        "target_hit": True,
                        "stop_hit": False,
                        "exit_date": "2026-06-03T09:25:00+05:30",
                        "exit_price": 105.0,
                        "exit_reason": "take_profit",
                        "quantity": 1000,
                        "gross_pnl": 5000.0,
                        "total_charges": 0.0,
                        "net_pnl": 5000.0,
                        "net_trade_return_pct": 5.0,
                    }],
                }
                for preset_id in preset_ids
            },
            "errors": {},
        }

    monkeypatch.setattr(server, "ProcessPoolExecutor", _RecordingProcessPool)
    monkeypatch.setattr(server, "as_completed", lambda futures: list(reversed(futures)))
    monkeypatch.setattr(server, "get_db", lambda: _Database())
    monkeypatch.setattr(server, "store", lambda _name: object())
    monkeypatch.setattr(server, "config_from_store", lambda _config: server.DeliveryChargeConfig())
    monkeypatch.setattr(server, "build_filter_presets", lambda: [preset])
    monkeypatch.setattr(server, "_parse_filter_expression", lambda _request: (
        None,
        {"5m": {"minimum_candles": 1, "warmup": 0, "required_fields": [], "requires_live": False}},
        [],
    ))
    monkeypatch.setattr(server, "_enhanced_momentum_scan_session_worker", scan_session)
    monkeypatch.setattr(server, "_enhanced_backtest_selected_stock_batch", overlay_batch)
    monkeypatch.setattr(server, "_enhanced_backtest_replay_stock", lambda *_args: {
        "stock_id": 101,
        "symbol": "ALPHA",
        "status": "completed",
        "return_pct": 5.0,
        "total_trades": 1,
        "trades": [{
            "stock_id": 101,
            "symbol": "ALPHA",
            "signal_date": sessions[0].isoformat(),
            "entry_date": sessions[1].isoformat(),
            "entry_price": 100.0,
            "target_price": 110.0,
            "stop_price": 90.0,
            "target_hit": True,
            "stop_hit": False,
            "exit_date": sessions[1].isoformat(),
            "exit_price": 110.0,
            "exit_reason": "target",
        }],
    })

    progress = []
    response = server._execute_enhanced_momentum_backtest(
        server.EnhancedMomentumBacktestRequest(
            end_date=sessions[-1],
            lookback_months=1,
            preset_ids=["mock_setup"],
            include_top_n_buy_hold=True,
            filters=server.EnhancedMomentumScanRequest(top_n=1),
        ),
        progress_callback=progress.append,
    )

    assert response["run"]["id"] == 77
    assert response["run"]["scan"]["cohort_mode"] == "scheduled_refresh"
    assert response["run"]["scan"]["cohort_date"] == sessions[0].isoformat()
    assert response["run"]["scan"]["snapshot_returned_stocks"] == 3
    assert response["run"]["scan"]["fresh_snapshot_stocks"] == 2
    assert response["run"]["scan"]["selected_ranked_stocks"] == 1
    assert response["run"]["scan"]["top_n_requested"] == 1
    assert response["run"]["scan"]["stale_snapshot_results_excluded"] == 1
    assert response["run"]["summary"]["returned_stocks"] == 1
    assert response["run"]["summary"]["scanner_snapshot_returned_stocks"] == 3
    assert response["run"]["scan"]["refresh_dates"] == [item.isoformat() for item in sessions]
    assert response["run"]["scan"]["capital_slots"] == 1
    assert response["run"]["scan"]["selected_builtin_filters"] == ["top_n_buy_hold"]
    assert response["run"]["scan"]["sessions"] == [
        {"date": sessions[0].isoformat(), "matching_stocks": 3, "returned_stocks": 3, "market_regime": sessions[0].isoformat()},
        {"date": sessions[1].isoformat(), "matching_stocks": 0, "returned_stocks": 0, "market_regime": sessions[1].isoformat()},
    ]
    assert response["run"]["scan"]["latest_market_context"]["regime"] == sessions[1].isoformat()
    assert scans == sessions
    assert overlay_gates == [{sessions[1].isoformat(): sessions[0].isoformat()}]
    assert [strategy["strategy_id"] for strategy in response["run"]["strategies"]] == [
        "enhanced_momentum",
        "enhanced_momentum_top_n_buy_hold",
        "mock_setup",
    ]
    assert response["run"]["strategies"][1]["stocks"][0]["stock_id"] == 101

    scan_pool, overlay_pool = _RecordingProcessPool.instances
    assert scan_pool.kwargs["max_workers"] == 2
    assert overlay_pool.kwargs["max_workers"] == 1
    assert overlay_pool.kwargs["mp_context"].get_start_method() == "spawn"
    assert overlay_pool.kwargs["initializer"] is server._initialize_cpu_analysis_worker
    assert len(overlay_pool.submissions) > 0
    assert any(item["phase"] == "Refreshed momentum cohort selected" and item["completed"] == 1 for item in progress)
    assert any(item["phase"] == "Evaluating selected replay setups" and item["completed"] == 1 for item in progress)


def test_replay_job_returns_pollable_progress_handle(monkeypatch):
    captured = []

    class _RecordingThread:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def start(self):
            return None

    class _JobTable:
        """Jobs are saved before the worker starts; keep that in memory here."""

        rows = {}

        def execute_returning(self, sql, params):
            assert "INSERT INTO enhanced_momentum_backtest_jobs" in sql
            self.rows[params["job_id"]] = dict(params)
            return {"job_id": params["job_id"]}

        def execute(self, sql, params):
            assert "UPDATE enhanced_momentum_backtest_jobs" in sql
            self.rows[params["job_id"]].update(params)
            return 1

        def fetch_all(self, _sql, _params=()):
            return [row for row in self.rows.values() if row["status"] in {"queued", "running"}]

    monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", {})
    monkeypatch.setattr(server, "get_db", lambda: _JobTable())
    monkeypatch.setattr(server, "build_filter_presets", lambda: [])
    monkeypatch.setattr(server, "Thread", _RecordingThread)

    queued = server.start_enhanced_momentum_backtest(
        server.EnhancedMomentumBacktestRequest(filters=server.EnhancedMomentumScanRequest())
    )
    assert queued["status"] == "queued"
    assert queued["progress"]["percent"] == 0
    assert captured[0]["target"] is server._run_enhanced_momentum_backtest_job
    server._update_enhanced_backtest_job(
        queued["job_id"],
        status="running",
        progress={
            "percent": 18,
            "phase": "Scanning historical sessions",
            "completed": 2,
            "total": 10,
            "unit": "sessions",
            "detail": "Finished a historical session",
        },
    )
    polled = server.get_enhanced_momentum_backtest_job(queued["job_id"])
    assert polled["status"] == "running"
    assert polled["progress"]["percent"] == 18
    assert polled["progress"]["completed"] == 2

    # Progress is monotonic even if a later phase reports a lower percentage.
    server._update_enhanced_backtest_job(
        queued["job_id"],
        progress={
            "percent": 10,
            "phase": "Preparing candle history",
            "completed": 0,
            "total": 0,
            "unit": "candles",
            "detail": "Loading candles",
        },
    )
    assert server.get_enhanced_momentum_backtest_job(queued["job_id"])["progress"]["percent"] == 18


def test_enhanced_backtest_filter_lookbacks_include_indicator_and_completeness_history():
    selected_ids = {
        "momentum_breakout",
        "trend_pullback",
        "volatility_breakout",
        "trend_continuation",
        "liquid_swing_momentum",
    }
    presets = [preset for preset in server.build_filter_presets() if preset.preset_id in selected_ids]

    assert server._enhanced_backtest_filter_lookbacks(presets) == {"1d": 205, "5m": 26}


def test_backtest_loader_reads_only_warmup_and_requested_window(monkeypatch):
    # Date-only backtest inputs are NSE session dates: the query boundaries
    # are exchange-time (Asia/Kolkata) midnights, so the 15:30 IST daily
    # close stays inside the requested day.
    start = datetime(2026, 6, 26, tzinfo=server.IST)
    end = datetime(2026, 9, 27, tzinfo=server.IST) - server.timedelta(microseconds=1)
    rows = [
        {
            "timestamp": datetime(2026, 6, 25, 9, tzinfo=timezone.utc),
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
        },
        {
            "timestamp": datetime(2026, 6, 26, 10, tzinfo=timezone.utc),
            "open": 101,
            "high": 103,
            "low": 100,
            "close": 102,
            "volume": 1200,
        },
    ]

    class _Database:
        queries = []

        def fetch_all(self, sql, params):
            self.queries.append((sql, params))
            return rows

    database = _Database()
    monkeypatch.setattr(server, "get_db", lambda: database)
    monkeypatch.setattr(server, "_resolve_stock_id", lambda *_args: {"id": 101, "symbol": "ALPHA"})

    # Pin "today" after the window: the loader clamps an end date that is
    # still in progress to the previous session, which must not apply here.
    with freeze_time("2026-10-01 12:00:00+05:30"):
        loaded = server._load_filter_backtest_inputs(
            server.FilterBacktestRequest(
                symbol="ALPHA",
                start_date="2026-06-26",
                end_date="2026-09-26",
            ),
            {"5m": {}},
            execution_timeframe=server.Timeframe.FIVE_MINUTE,
            warmup_candles_by_timeframe={"5m": 50},
        )

    sql, params = database.queries[0]
    assert "WITH warmup AS" in sql
    assert "ORDER BY timestamp DESC" in sql
    assert "LIMIT %s" in sql
    assert "timestamp >= %s AND timestamp <= %s" in sql
    assert params == (101, start, 50, 101, start, end)
    points = loaded[4][server.Timeframe.FIVE_MINUTE]
    assert [point.timestamp for point in points] == [row["timestamp"] for row in rows]
