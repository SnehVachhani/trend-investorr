"""PostgreSQL-backed checks for the Enhanced Momentum replay job storage.

These run only when a throwaway PostgreSQL is reachable via
``ALGOANALYST_TEST_PG_HOST`` (plus optional ``_PORT``/``_USER``/``_PASSWORD``).
Each test gets a fresh database and drops it afterwards. No production data,
no broker credentials, nothing outside that scratch server.
"""

import multiprocessing
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg2
import pytest

from api import server
from storage.database import Database
from tests.api import _replay_process_harness as harness

pytestmark = pytest.mark.integration

_HOST = os.environ.get("ALGOANALYST_TEST_PG_HOST")
_PORT = int(os.environ.get("ALGOANALYST_TEST_PG_PORT", "5432"))
_USER = os.environ.get("ALGOANALYST_TEST_PG_USER", "postgres")
_PASSWORD = os.environ.get("ALGOANALYST_TEST_PG_PASSWORD", "")


@pytest.fixture
def pg_scratch():
    """A fresh database for one test: ``.db`` (connected) and ``.dsn`` for other processes."""
    if not _HOST:
        pytest.skip("set ALGOANALYST_TEST_PG_HOST to run PostgreSQL-backed checks")
    name = f"algo_test_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(host=_HOST, port=_PORT, user=_USER, password=_PASSWORD, dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    dsn = {"host": _HOST, "port": _PORT, "dbname": name, "user": _USER, "password": _PASSWORD}
    db = Database(**dsn)
    db.connect()
    try:
        yield SimpleNamespace(db=db, dsn=dsn)
    finally:
        db.disconnect() if hasattr(db, "disconnect") else None
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def pg_database(pg_scratch):
    return pg_scratch.db


def _result_index(db):
    return db.fetch_one(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'enhanced_momentum_backtest_runs' "
        "AND indexname = 'idx_enhanced_momentum_backtests_job'"
    )


def _insert_run(db, job_id):
    return int(db.execute_returning(
        "INSERT INTO enhanced_momentum_backtest_runs (signal_cutoff_date, end_date, job_id) "
        "VALUES ('2026-06-29', '2026-06-30', %s) RETURNING id",
        (job_id,),
    )["id"])


def test_result_index_upgrade_keeps_every_saved_result_and_relinks_one_per_job(pg_database):
    db = pg_database
    db.run_migrations()

    # Recreate the early environment: the non-unique result index, plus the
    # duplicates it allowed - two results for one job, the job pointing at
    # the second - and an unrelated single result.
    db.execute("DROP INDEX idx_enhanced_momentum_backtests_job")
    db.execute("CREATE INDEX idx_enhanced_momentum_backtests_job ON enhanced_momentum_backtest_runs(job_id)")
    assert "UNIQUE" not in _result_index(db)["indexdef"]
    older = _insert_run(db, "jobdup")
    newer = _insert_run(db, "jobdup")
    single = _insert_run(db, "jobsingle")
    db.execute(
        "INSERT INTO enhanced_momentum_backtest_jobs (job_id, status, run_id, created_at) "
        "VALUES ('jobdup', 'completed', %s, %s)",
        (newer, datetime.now(timezone.utc)),
    )

    db.run_migrations()

    index = _result_index(db)["indexdef"]
    assert index.startswith("CREATE UNIQUE INDEX"), index
    assert "WHERE (job_id IS NOT NULL)" in index, index
    rows = {int(r["id"]): r["job_id"] for r in db.fetch_all("SELECT id, job_id FROM enhanced_momentum_backtest_runs")}
    assert set(rows) == {older, newer, single}, "no saved result was discarded"
    assert rows[newer] == "jobdup", "the result the job references keeps the link"
    assert rows[older] is None, "the duplicate is unlinked, not deleted"
    assert rows[single] == "jobsingle"
    assert db.fetch_one("SELECT run_id FROM enhanced_momentum_backtest_jobs WHERE job_id = 'jobdup'")["run_id"] == newer

    # Re-running is a no-op that still verifies the index.
    db.run_migrations()
    assert _result_index(db)["indexdef"] == index
    with pytest.raises(Exception):
        _insert_run(db, "jobdup")

    # A missing index is rebuilt too, and the unlinked duplicate stays.
    db.execute("DROP INDEX idx_enhanced_momentum_backtests_job")
    db.run_migrations()
    assert _result_index(db)["indexdef"] == index
    assert db.fetch_one("SELECT COUNT(*) AS n FROM enhanced_momentum_backtest_runs")["n"] == 3


def test_job_store_statements_behave_on_postgres(monkeypatch, pg_database):
    """The admission, fence, settle and fenced-save statements, for real."""
    db = pg_database
    db.run_migrations()
    monkeypatch.setattr(server, "get_db", lambda: db)
    request = server.EnhancedMomentumBacktestRequest(submission_id="pg-1", mode="buy_and_hold")
    now = datetime.now(timezone.utc)

    def job(job_id, submission_id):
        return {
            "job_id": job_id, "submission_id": submission_id, "status": "queued", "created_at": now,
            "request_fingerprint": server._enhanced_backtest_request_fingerprint(request),
            "progress": {"percent": 0}, "run_id": None, "error": None,
        }

    assert server._admit_enhanced_backtest_job(job("j1", "pg-1"), request) == "admitted"
    assert server._admit_enhanced_backtest_job(job("j2", "pg-2"), request) == "busy"

    # Owner fence: right owner writes, wrong owner is rejected.
    assert server._persist_enhanced_backtest_job_changes("j1", {"status": "running"}) == "written"
    monkeypatch.setattr(server, "_ENHANCED_BACKTEST_WORKER_ID", "someone-else")
    assert server._persist_enhanced_backtest_job_changes("j1", {"status": "failed"}) == "rejected"
    assert server._load_enhanced_backtest_job("j1")["status"] == "running"

    # Fenced, idempotent result save: the wrong owner cannot save; the right
    # owner's repeat save returns the same run without inserting another.
    values = ("2026-06-29", "2026-06-30", 1, "{}", "{}", "{}", "{}", "[]", "[]")
    with pytest.raises(server._EnhancedBacktestOwnershipLost):
        server._save_enhanced_backtest_run(db, values, job_id="j1")
    monkeypatch.undo()
    monkeypatch.setattr(server, "get_db", lambda: db)
    first = server._save_enhanced_backtest_run(db, values, job_id="j1")
    second = server._save_enhanced_backtest_run(db, values, job_id="j1")
    assert first["id"] == second["id"]
    assert db.fetch_one("SELECT COUNT(*) AS n FROM enhanced_momentum_backtest_runs")["n"] == 1

    # Once the slot is free, reusing an identity trips the identity index.
    assert server._persist_enhanced_backtest_job_changes("j1", {"status": "completed", "run_id": first["id"]}) == "written"
    assert server._admit_enhanced_backtest_job(job("j3", "pg-1"), request) == "duplicate"
    assert server._admit_enhanced_backtest_job(job("j4", "pg-4"), request) == "admitted"


# ── Genuinely separate API processes sharing the scratch database ──────────


def _seed_replay_data(db):
    """Migrations plus the minimum the buy-and-hold replay reads: one stock
    universe with daily candles on the snapshot and end sessions."""
    db.run_migrations()
    for stock_id in (1, 2, 3):
        db.execute(
            "INSERT INTO stocks (id, symbol, token, name) VALUES (%s, %s, %s, %s)",
            (stock_id, f"STOCK{stock_id}", str(1000 + stock_id), f"Stock {stock_id}"),
        )
        for day, px in ((harness.SNAPSHOT_DATE, 100.0), (harness.END_DATE, 110.0)):
            db.execute(
                "INSERT INTO candles_1d (stock_id, timestamp, open, high, low, close, volume) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (stock_id, datetime.combine(day, datetime.min.time().replace(hour=15, minute=30), tzinfo=server.IST),
                 px, px + 1, px - 1, px, 1000),
            )


def _run_processes(specs, timeout=90, between=None):
    """Start one spawned process per spec and collect every report.

    ``between`` runs in the parent once the processes are started, for
    scenarios the parent must steer while the children are mid-flight.
    """
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    processes = []
    for spec in specs:
        kwargs = dict(spec)
        name = kwargs.pop("name")
        process = ctx.Process(target=harness.submit, args=(name, kwargs.pop("dsn"), kwargs.pop("identity"), results), kwargs=kwargs, name=name)
        process.start()
        processes.append(process)
    if between is not None:
        between()
    reports = {}
    deadline = time.monotonic() + timeout
    while len(reports) < len(processes) and time.monotonic() < deadline:
        try:
            report = results.get(timeout=1)
        except Exception:  # noqa: BLE001 - queue.Empty
            continue
        reports[report["process"]] = report
    for process in processes:
        process.join(10)
        if process.is_alive():
            process.kill()
    assert len(reports) == len(processes), f"missing reports: {reports}"
    crashed = {name: r["crashed"] for name, r in reports.items() if "crashed" in r}
    assert not crashed, crashed
    return reports


def _jobs(db):
    return {row["job_id"]: row for row in db.fetch_all(
        "SELECT job_id, submission_id, status, worker_id, run_id, error, heartbeat_at FROM enhanced_momentum_backtest_jobs"
    )}


def _runs(db):
    return db.fetch_all("SELECT id, job_id FROM enhanced_momentum_backtest_runs ORDER BY id")


def test_two_processes_submitting_different_replays_at_once_admit_exactly_one(pg_scratch):
    """Both processes submit at the same instant; the winner's replay is held
    open until both admission attempts have returned, so the attempts are
    guaranteed to overlap rather than merely likely to."""
    db, dsn = pg_scratch.db, pg_scratch.dsn
    _seed_replay_data(db)
    ctx = multiprocessing.get_context("spawn")
    together = ctx.Barrier(2)
    admission_done = ctx.Barrier(3)          # alpha, beta and this parent
    entered, resume = ctx.Event(), ctx.Event()
    observed_during_overlap = {}

    def while_both_admissions_settle():
        admission_done.wait(60)
        # Both attempts have returned and the winner's worker is parked inside
        # the replay: whatever storage says now was decided under overlap.
        assert entered.wait(30), "the admitted replay never started running"
        observed_during_overlap.update(_jobs(db))
        resume.set()

    reports = _run_processes(
        [
            {"name": "alpha", "dsn": dsn, "identity": "sub-alpha", "barrier": together,
             "admission_done": admission_done, "stall_entered": entered, "stall_resume": resume},
            {"name": "beta", "dsn": dsn, "identity": "sub-beta", "barrier": together,
             "admission_done": admission_done, "stall_entered": entered, "stall_resume": resume},
        ],
        between=while_both_admissions_settle,
    )

    admitted = [r for r in reports.values() if "response" in r]
    refused = [r for r in reports.values() if "refused" in r]
    assert len(admitted) == 1 and len(refused) == 1, reports
    assert refused[0]["refused"] == 409
    winner = admitted[0]
    assert winner["owned"] is True
    assert list(observed_during_overlap) == [winner["response"]["job_id"]]
    assert observed_during_overlap[winner["response"]["job_id"]]["status"] == "running", (
        "the winning replay was still open when the losing attempt was refused"
    )
    assert winner["final"]["status"] == "completed" and winner["final"]["run_id"] is not None

    jobs = _jobs(db)
    assert list(jobs) == [winner["response"]["job_id"]], "storage admitted exactly one job"
    job = jobs[winner["response"]["job_id"]]
    assert job["status"] == "completed" and job["worker_id"] == winner["worker_id"]
    runs = _runs(db)
    assert [r["job_id"] for r in runs] == [job["job_id"]] and job["run_id"] == runs[0]["id"]
    assert winner["save_log"] == [f"saved-run-{runs[0]['id']}"]


def test_two_processes_retrying_one_submission_at_once_converge_on_one_job(pg_scratch):
    db, dsn = pg_scratch.db, pg_scratch.dsn
    _seed_replay_data(db)
    ctx = multiprocessing.get_context("spawn")
    together = ctx.Barrier(2)
    admission_done = ctx.Barrier(3)
    entered, resume = ctx.Event(), ctx.Event()
    observed_during_overlap = {}

    def while_both_admissions_settle():
        admission_done.wait(60)
        assert entered.wait(30), "the admitted replay never started running"
        observed_during_overlap.update(_jobs(db))
        resume.set()

    reports = _run_processes(
        [
            {"name": "first", "dsn": dsn, "identity": "sub-shared", "barrier": together,
             "admission_done": admission_done, "stall_entered": entered, "stall_resume": resume},
            {"name": "second", "dsn": dsn, "identity": "sub-shared", "barrier": together,
             "admission_done": admission_done, "stall_entered": entered, "stall_resume": resume},
        ],
        between=while_both_admissions_settle,
    )

    assert all("response" in r for r in reports.values()), f"a retry must never be refused: {reports}"
    job_ids = {r["response"]["job_id"] for r in reports.values()}
    assert len(job_ids) == 1, "both callers hold the same job"
    assert list(observed_during_overlap) == list(job_ids)
    assert next(iter(observed_during_overlap.values()))["status"] == "running", (
        "the original replay was still open when the retry was answered with it"
    )
    owners = [r for r in reports.values() if r["owned"]]
    bystanders = [r for r in reports.values() if not r["owned"]]
    assert len(owners) == 1 and len(bystanders) == 1, reports
    assert owners[0]["final"]["status"] == "completed"
    assert bystanders[0]["registry"] == {}, "the loser runs no worker"
    assert bystanders[0]["save_log"] == []
    assert bystanders[0]["response"]["submission_id"] == "sub-shared"

    jobs = _jobs(db)
    assert len(jobs) == 1 and len(_runs(db)) == 1
    only = next(iter(jobs.values()))
    assert only["status"] == "completed" and only["worker_id"] == owners[0]["worker_id"]


@pytest.mark.parametrize(
    "detector, heartbeat_interval, resume_after_forfeit, expected_save_log",
    [
        # The thawed worker's heartbeat is refused first (the worker waits
        # for that); it forfeits the job and the replay aborts at its next
        # progress report, before ever reaching the save.
        ("heartbeat", 0.5, True, []),
        # With the heartbeat effectively silent, the worker runs on to the
        # result save itself, which the storage fence refuses.
        ("result-save", 1_000.0, False, ["save-rejected-ownership-lost"]),
    ],
)
def test_expired_worker_resuming_after_replacement_cannot_publish(
    monkeypatch, pg_scratch, detector, heartbeat_interval, resume_after_forfeit, expected_save_log
):
    """Process A's worker is frozen mid-replay past the heartbeat timeout.
    This (parent) process settles its job and admits a replacement. A is
    thawed and tries to finish: its result save and outcome writes must be
    refused, and the replacement left alone."""
    db, dsn = pg_scratch.db, pg_scratch.dsn
    _seed_replay_data(db)
    ctx = multiprocessing.get_context("spawn")
    entered, resume = ctx.Event(), ctx.Event()
    results = ctx.Queue()
    heartbeat_timeout = 2.0

    worker = ctx.Process(
        target=harness.submit,
        args=("expiring", dsn, "sub-expiring", results),
        kwargs={
            "stall_entered": entered, "stall_resume": resume,
            # Heartbeats are the only thing keeping a stalled job alive; a
            # frozen process cannot send them, which is the whole point.
            "heartbeat_interval": heartbeat_interval, "heartbeat_timeout": heartbeat_timeout,
            "resume_after_forfeit": resume_after_forfeit,
        },
        name="expiring",
    )
    worker.start()
    try:
        assert entered.wait(60), "worker never reached the replay"
        jobs = _jobs(db)
        assert len(jobs) == 1
        (job_id, row), = jobs.items()
        assert row["status"] == "running"
        worker_id = row["worker_id"]

        os.kill(worker.pid, signal.SIGSTOP)          # the process is frozen: no heartbeats
        time.sleep(heartbeat_timeout + 1.0)

        # This process acts as the other API worker.
        monkeypatch.setattr(server, "get_db", lambda: db)
        monkeypatch.setattr(server, "_ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT", harness.timedelta(seconds=heartbeat_timeout))
        monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", {})
        settled = server.get_enhanced_momentum_backtest_job(job_id)
        assert settled["status"] == "failed" and "interrupted" in settled["error"], settled

        class _ParkedThread:
            def __init__(self, **_kwargs): pass
            def start(self): return None

        monkeypatch.setattr(server, "Thread", _ParkedThread)
        monkeypatch.setattr(server, "store", lambda _name: harness._NullConfigStore())
        replacement = server.start_enhanced_momentum_backtest(harness.request_for(server, "sub-replacement"))
        replacement_before = _jobs(db)[replacement["job_id"]]
        assert replacement_before["status"] == "queued" and replacement_before["worker_id"] == server._ENHANCED_BACKTEST_WORKER_ID

        os.kill(worker.pid, signal.SIGCONT)          # thawed: the stale worker resumes
        resume.set()
        report = results.get(timeout=60)
    finally:
        resume.set()
        try:
            os.kill(worker.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        worker.join(10)
        if worker.is_alive():
            worker.kill()

    assert "crashed" not in report, report
    assert report["worker_id"] == worker_id
    assert report["final"]["status"] == "failed" and "interrupted" in report["final"]["error"], (
        "the expired worker must end up seeing the settled outcome, not its own"
    )
    assert report["registry"] == {}, "a fenced-out worker forfeits the job"
    assert report["save_log"] == expected_save_log, (
        f"[{detector}] unexpected publish attempts by the stale worker: {report['save_log']}"
    )

    jobs_after = _jobs(db)
    expired = jobs_after[job_id]
    assert expired["status"] == "failed" and expired["run_id"] is None and "interrupted" in expired["error"]
    assert _runs(db) == [], "no late result was saved"
    replacement_after = jobs_after[replacement["job_id"]]
    assert replacement_after == replacement_before, "the replacement was not touched by the stale worker"
