"""Tests for API startup candle synchronization."""

import sys
import types
from pathlib import Path

from api import server


class FakeIngestion:
    def __init__(self, result=None, error=None):
        self.result = result or {"ONE_DAY": {"candles_stored": 4}}
        self.error = error
        self.calls = 0

    def sync_pending_all_intervals(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def backfill_all_intervals(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def reset_sync_state():
    server._candle_sync_state.update(
        {
            "status": "idle",
            "started_at": None,
            "completed_at": None,
            "stats": None,
            "error": None,
        }
    )


def test_startup_sync_backfills_all_intervals_and_records_success(monkeypatch):
    reset_sync_state()
    ingestion = FakeIngestion()
    monkeypatch.setattr(server, "get_ingestion", lambda: ingestion)

    server._run_startup_candle_sync()

    assert ingestion.calls == 1
    assert server._candle_sync_state["status"] == "completed"
    assert server._candle_sync_state["stats"] == ingestion.result
    assert server._candle_sync_state["error"] is None


def test_startup_sync_records_failure_without_raising(monkeypatch):
    reset_sync_state()
    ingestion = FakeIngestion(error=RuntimeError("broker unavailable"))
    monkeypatch.setattr(server, "get_ingestion", lambda: ingestion)

    server._run_startup_candle_sync()

    assert ingestion.calls == 1
    assert server._candle_sync_state["status"] == "failed"
    assert server._candle_sync_state["stats"] is None
    assert "broker unavailable" in server._candle_sync_state["error"]


def test_startup_sync_does_not_launch_duplicate_when_running(monkeypatch):
    reset_sync_state()
    server._candle_sync_state["status"] = "running"
    started = []

    class UnexpectedThread:
        def __init__(self, *args, **kwargs):
            started.append((args, kwargs))

    monkeypatch.setattr(server, "Thread", UnexpectedThread)

    server._start_startup_candle_sync()

    assert started == []


def test_startup_sync_uses_coverage_aware_backfill_for_historical_gaps(monkeypatch):
    reset_sync_state()
    calls = []

    class HistoricalGapIngestion:
        def backfill_all_intervals(self):
            calls.append("backfill_all_intervals")
            return {"ONE_DAY": {"candles_stored": 12}}

        def sync_pending_all_intervals(self):
            calls.append("sync_pending_all_intervals")
            raise AssertionError("startup sync must fill historical gaps")

    monkeypatch.setattr(server, "get_ingestion", lambda: HistoricalGapIngestion())

    server._run_startup_candle_sync()

    assert calls == ["backfill_all_intervals"]
    assert server._candle_sync_state["status"] == "completed"


def test_get_ingestion_passes_audit_trail_to_smartapi_session(monkeypatch):
    captured = {}

    class FakeSettings:
        vault_dir = Path("vault")
        audit_dir = Path("audit")
        VAULT_MASTER_KEY = ""

    class FakeVault:
        def __init__(self, vault_path, master_key):
            captured["vault_path"] = vault_path
            captured["master_key"] = master_key

    class FakeAuditTrail:
        def __init__(self, log_dir):
            captured["audit_dir"] = log_dir

    class FakeSession:
        def __init__(self, vault, audit):
            captured["session_vault"] = vault
            captured["session_audit"] = audit

        def login(self):
            captured["logged_in"] = True

    class FakeFetcher:
        def __init__(self, session):
            captured["fetcher_session"] = session

    class FakeIngestion:
        def __init__(self, **kwargs):
            captured["ingestion_kwargs"] = kwargs

    def fake_module(**objects):
        module = types.ModuleType("fake")
        for name, value in objects.items():
            setattr(module, name, value)
        return module

    monkeypatch.setattr(server, "Settings", FakeSettings)
    monkeypatch.setattr(server, "store", lambda name: name)
    monkeypatch.setattr(server, "get_db", lambda: object())
    monkeypatch.setattr(server, "_ingestion", None)
    monkeypatch.setitem(
        sys.modules,
        "data.data_ingestion",
        fake_module(DataIngestionService=FakeIngestion),
    )
    monkeypatch.setitem(
        sys.modules,
        "data.fetchers.historical_fetcher",
        fake_module(HistoricalFetcher=FakeFetcher),
    )
    monkeypatch.setitem(
        sys.modules,
        "data.auth.smartapi_auth",
        fake_module(SmartAPISession=FakeSession),
    )
    monkeypatch.setitem(
        sys.modules,
        "core.security.vault",
        fake_module(SecureVault=FakeVault),
    )
    monkeypatch.setitem(
        sys.modules,
        "core.security.audit",
        fake_module(AuditTrail=FakeAuditTrail),
    )

    ingestion = server.get_ingestion()

    assert isinstance(ingestion, FakeIngestion)
    assert captured["vault_path"] == FakeSettings.vault_dir
    assert captured["master_key"] == FakeSettings.VAULT_MASTER_KEY
    assert captured["audit_dir"] == FakeSettings.audit_dir
    assert captured["session_vault"].__class__ is FakeVault
    assert captured["session_audit"].__class__ is FakeAuditTrail
    assert captured["logged_in"] is True


def test_system_status_reports_live_candle_table_counts(monkeypatch):
    class LiveCountDatabase:
        def health_check(self):
            return True

        def fetch_one(self, query):
            return {"cnt": 3_377}

        def fetch_all(self, query):
            assert "COUNT(DISTINCT stock_id)" in query
            assert "COUNT(*)" in query
            assert "candles_1h" in query
            return [
                {
                    "interval": "ONE_HOUR",
                    "stock_count": 4,
                    "global_earliest": None,
                    "global_latest": None,
                    "total_candles": 1_802_741,
                }
            ]

    monkeypatch.setattr(server, "get_db", lambda: LiveCountDatabase())

    result = server.get_system_status()

    assert result["coverage"] == [
        {"interval": "ONE_HOUR", "stocks": 4, "candles": 1_802_741}
    ]
