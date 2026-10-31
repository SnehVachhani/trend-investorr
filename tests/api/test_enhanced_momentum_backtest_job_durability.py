"""Regression: an Enhanced Momentum replay job must survive an API process restart.

The UI starts a replay with POST /api/enhanced-momentum-scanner/backtest, gets a
``job_id`` back, and polls GET /backtest-jobs/{job_id} until it sees ``run_id``.
If the API process restarts in between, the caller must still be able to trust
the handle it was given: either it resolves to the saved run, or it reports a
terminal state. A bare 404 leaves the caller unable to tell "finished and saved"
from "killed before saving".

The replay work itself is stubbed (no scanner, no candles beyond a fixture), but
the real job lifecycle and the real persistence code path run unchanged. Nothing
here needs broker credentials, PostgreSQL, or production data.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Lock

import pytest
from fastapi import HTTPException
from freezegun import freeze_time

from api import server
from core.exceptions import DatabaseError


SNAPSHOT_DATE = date(2026, 6, 29)
END_DATE = date(2026, 6, 30)
SAVED_RUN_ID = 4242


class _FakeRunStore:
    """Stands in for PostgreSQL: session dates, candles, the runs table and
    the jobs table.

    Unlike the in-process job registry, whatever is written here survives a
    process restart, so the same instance is shared by every simulated API
    process. Each statement is applied atomically under one lock, the way a
    single autocommit statement is in PostgreSQL:

    * the admission insert honours its ``WHERE NOT EXISTS`` guard;
    * an ``UPDATE`` only touches rows matching its ``job_id``/``owner_id``/
      ``status``/``heartbeat_before`` predicates and reports the row count;
    * the fenced run save (``WITH owner AS (UPDATE ...) INSERT ...``) saves
      nothing unless the job is still running under the given worker.

    ``stale_reads`` models reads that ran before a concurrent insert landed:
    the unfinished-job listing and submission lookups are frozen as seen at
    entry. A rejected admission is where a process observes the other commit
    (read committed: each statement sees what is committed when it starts),
    so the freeze ends there. ``lose_next_admission_response`` /
    ``lose_next_run_save_response`` model a statement that commits but whose
    reply never reaches the client.
    """

    ACTIVE = {"queued", "running"}

    def __init__(self):
        self.runs: dict[int, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.inserted_params: list[tuple] = []
        self.job_writes: list[tuple[str, dict]] = []
        self._next_id = SAVED_RUN_ID
        self._lock = Lock()
        self._frozen_jobs: dict[str, dict] | None = None
        self.lose_next_admission_response = False
        self.lose_next_run_save_response = False
        self.fail_next_run_saves = 0      # transport errors before anything is written
        self.fail_next_run_lookups = 0    # transport errors on "is there a run for this job?"
        self._identity_misses: dict[str, int] = {}

    def race_identity_lookups(self, submission_id, misses=1):
        """The next ``misses`` lookups of this identity ran before the
        concurrent admission committed and therefore find nothing."""
        self._identity_misses[submission_id] = misses

    @contextmanager
    def stale_reads(self):
        self._frozen_jobs = {job_id: dict(row) for job_id, row in self.jobs.items()}
        try:
            yield
        finally:
            self._frozen_jobs = None

    def _visible_jobs(self):
        return self._frozen_jobs if self._frozen_jobs is not None else self.jobs

    def _active_rows(self, table=None):
        table = self.jobs if table is None else table
        return [dict(row) for row in table.values() if row["status"] in self.ACTIVE]

    # ── reads ────────────────────────────────────────────────────────────
    def fetch_all(self, sql, params=()):
        if "SELECT DISTINCT" in sql:
            return [{"session_date": SNAPSHOT_DATE}, {"session_date": END_DATE}]
        if "FROM enhanced_momentum_backtest_jobs" in sql:
            assert "status IN ('queued', 'running')" in sql
            return self._active_rows(self._visible_jobs())
        if "FROM enhanced_momentum_backtest_runs" in sql:
            return list(self.runs.values())
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

    def fetch_one(self, sql, params):
        if "FROM enhanced_momentum_backtest_jobs" in sql:
            if "submission_id" in params:
                assert "WHERE submission_id = %(submission_id)s" in sql
                if self._identity_misses.get(params["submission_id"], 0) > 0:
                    self._identity_misses[params["submission_id"]] -= 1
                    return None
                matches = [row for row in self._visible_jobs().values()
                           if row.get("submission_id") == params["submission_id"]]
                return dict(matches[0]) if matches else None
            row = self.jobs.get(params["job_id"])
            return dict(row) if row else None
        assert "FROM enhanced_momentum_backtest_runs" in sql
        if "WHERE job_id" in sql:
            if self.fail_next_run_lookups > 0:
                self.fail_next_run_lookups -= 1
                raise DatabaseError("connection reset during result lookup")
            matches = [row for row in self.runs.values() if row.get("job_id") == params[0]]
            return max(matches, key=lambda row: row["created_at"]) if matches else None
        return self.runs.get(int(params[0]))

    # ── writes ───────────────────────────────────────────────────────────
    def _matches(self, row, sql, params):
        if row is None:
            return False
        if "owner_id" in params and row.get("worker_id") != params["owner_id"]:
            return False
        if "status IN ('queued', 'running')" in sql and row["status"] not in self.ACTIVE:
            return False
        if "status = 'running'" in sql and row["status"] != "running":
            return False
        if "heartbeat_before" in params:
            seen = row.get("heartbeat_at") or row["created_at"]
            if not seen < params["heartbeat_before"]:
                return False
        return True

    def execute(self, sql, params):
        assert isinstance(params, dict), "jobs table statements use named parameters"
        with self._lock:
            if "INSERT INTO enhanced_momentum_backtest_jobs" in sql:
                # Plain insert: no admission guard (the pre-fix statement).
                assert params["job_id"] not in self.jobs
                self.jobs[params["job_id"]] = {k: v for k, v in params.items()}
                self.job_writes.append(("insert", dict(params)))
                return 1
            if "UPDATE enhanced_momentum_backtest_jobs" in sql:
                predicates = {"job_id", "owner_id", "heartbeat_before"}
                row = self.jobs.get(params["job_id"])
                if not self._matches(row, sql, params):
                    self.job_writes.append(("update-rejected", dict(params)))
                    return 0
                for column, value in params.items():
                    if column not in predicates:
                        assert f"{column} = %({column})s" in sql, f"{column} not in SET clause"
                        row[column] = value
                self.job_writes.append(("update", dict(params)))
                return 1
            raise AssertionError(f"unexpected statement: {sql[:80]}")

    def execute_returning(self, sql, params):
        with self._lock:
            if "INSERT INTO enhanced_momentum_backtest_jobs" in sql:
                assert "WHERE NOT EXISTS" in sql and "RETURNING job_id" in sql
                if self._active_rows():
                    self.job_writes.append(("admission-rejected", dict(params)))
                    self._frozen_jobs = None
                    return None
                submission_id = params.get("submission_id")
                if submission_id is not None and any(
                    row.get("submission_id") == submission_id for row in self.jobs.values()
                ):
                    # The partial unique index on submission_id fires.
                    self._frozen_jobs = None
                    self.job_writes.append(("admission-duplicate", dict(params)))
                    raise DatabaseError(
                        'duplicate key value violates unique constraint '
                        '"idx_enhanced_momentum_backtest_jobs_submission"'
                    )
                self.jobs[params["job_id"]] = dict(params)
                self.job_writes.append(("insert", dict(params)))
                if self.lose_next_admission_response:
                    self.lose_next_admission_response = False
                    raise DatabaseError("connection reset after commit")
                return {"job_id": params["job_id"]}
            assert "INSERT INTO enhanced_momentum_backtest_runs" in sql
            if self.fail_next_run_saves > 0:
                self.fail_next_run_saves -= 1
                self.job_writes.append(("run-save-transport-error", {"job_id": params[-1]}))
                raise DatabaseError("connection reset before the statement was sent")
            columns = [
                "signal_cutoff_date", "end_date", "lookback_months", "filters_json",
                "config_json", "summary_json", "scan_json", "results_json", "strategies_json", "job_id",
            ]
            if sql.lstrip().startswith("WITH owner AS"):
                # Placeholders are positional in statement order: the CTE's
                # UPDATE predicate first, then the inserted values.
                job_id, owner_id = params[:2]
                values = params[2:]
                assert len(values) == len(columns) and values[-1] == job_id
                row = self.jobs.get(job_id)
                if not self._matches(row, "status = 'running'", {"owner_id": owner_id}):
                    self.job_writes.append(("run-save-rejected", {"job_id": job_id, "owner_id": owner_id}))
                    return None
                row["heartbeat_at"] = datetime.now(timezone.utc)
            else:
                values = params
                assert len(values) == len(columns), "run insert must carry the job id"
            job_id = values[-1]
            if job_id is not None and any(run.get("job_id") == job_id for run in self.runs.values()):
                # Partial unique index on runs(job_id): ON CONFLICT DO NOTHING.
                assert "ON CONFLICT (job_id) WHERE job_id IS NOT NULL DO NOTHING" in sql
                self.job_writes.append(("run-save-conflict", {"job_id": job_id}))
                return None
            run_id = self._next_id
            self._next_id += 1
            created_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
            self.inserted_params.append(values)
            run = dict(zip(columns, values))
            run.update({"id": run_id, "created_at": created_at})
            self.runs[run_id] = run
            if self.lose_next_run_save_response:
                self.lose_next_run_save_response = False
                raise DatabaseError("connection reset after commit")
            return {"id": run_id, "created_at": created_at}


class _InlineThread:
    """Run the replay worker synchronously so the test is deterministic."""

    def __init__(self, target, args=(), **_kwargs):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class _NeverStartedThread:
    """Model a worker that the dying process never gets to finish."""

    def __init__(self, **_kwargs):
        pass

    def start(self):
        return None


class _UnstartableThread:
    """Model the process hitting a thread limit: ``start()`` raises."""

    attempts = 0

    def __init__(self, **_kwargs):
        pass

    def start(self):
        type(self).attempts += 1
        raise RuntimeError("can't start new thread")


def _stub_replay_work(monkeypatch, run_store):
    """Replace the scanner and config lookups; keep the job + save path real."""
    candidates = [
        {
            "stock_id": stock_id,
            "symbol": f"STOCK{stock_id}",
            "name": f"Stock {stock_id}",
            "company_name": f"Stock {stock_id} Ltd",
            "industry": "Industrials",
            "as_of_date": SNAPSHOT_DATE.isoformat(),
            "rank": stock_id,
            "enhanced_score": 100.0 - stock_id,
        }
        for stock_id in (1, 2, 3)
    ]

    scan_calls = []

    def scan_session(_session_date, _filters):
        scan_calls.append(_session_date)
        return {
            "matching_stocks": len(candidates),
            "returned_stocks": len(candidates),
            "market_context": {"regime": "neutral"},
            "formula": {},
            "results": candidates,
        }

    monkeypatch.setattr(server, "get_db", lambda: run_store)
    monkeypatch.setattr(server, "store", lambda _name: object())
    monkeypatch.setattr(server, "config_from_store", lambda _config: server.DeliveryChargeConfig(enabled=False))
    monkeypatch.setattr(server, "_enhanced_momentum_scan_session_worker", scan_session)
    monkeypatch.setattr(server, "build_filter_presets", lambda: [])
    return scan_calls


class _ApiProcess:
    """Module state of one API process: its own job registry, lock, worker
    identity and thread implementation. Storage is shared between processes.
    """

    def __init__(self, name, thread_cls):
        self.name = name
        self.jobs = {}
        self.lock = Lock()
        self.worker_id = f"{name}:{uuid.uuid4().hex[:8]}"
        self.thread_cls = thread_cls

    def activate(self, monkeypatch, run_store):
        monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", self.jobs)
        monkeypatch.setattr(server, "_enhanced_momentum_backtest_lock", self.lock)
        monkeypatch.setattr(server, "_ENHANCED_BACKTEST_WORKER_ID", self.worker_id)
        monkeypatch.setattr(server, "Thread", self.thread_cls)
        monkeypatch.setattr(server, "get_db", lambda: run_store)
        return self


def _boot_fresh_api_process(monkeypatch, run_store):
    """Model a fresh API process after a restart or crash.

    ``api.server`` is imported again in a new process, so every module-level
    global is re-created from scratch: the job registry is an empty dict, the
    lock and worker identity are new. The database is the only carry-over.
    """
    return _ApiProcess("restarted", _NeverStartedThread).activate(monkeypatch, run_store)


def _request(submission_id=None, top_n=30):
    return server.EnhancedMomentumBacktestRequest(
        submission_id=submission_id,
        start_date=SNAPSHOT_DATE,
        end_date=END_DATE,
        mode="buy_and_hold",
        top_n=top_n,
        capital_per_stock=100_000,
        filters=server.EnhancedMomentumScanRequest(max_results=1),
    )


def _poll(job_id):
    try:
        return server.get_enhanced_momentum_backtest_job(job_id)
    except HTTPException as error:
        return {"status": f"http_{error.status_code}", "detail": error.detail, "run_id": None}


def test_completed_replay_job_still_resolves_to_its_saved_run_after_api_restart(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", {})
    monkeypatch.setattr(server, "Thread", _InlineThread)

    # ── Process 1: submit, run to completion, observe the handle ──────────
    queued = server.start_enhanced_momentum_backtest(_request())
    job_id = queued["job_id"]

    # The job and its request were recorded before the worker ran anything.
    first_write = run_store.job_writes[0]
    assert first_write[0] == "insert" and first_write[1]["status"] == "queued"
    assert json.loads(first_write[1]["request_json"])["mode"] == "buy_and_hold"

    before_restart = server.get_enhanced_momentum_backtest_job(job_id)
    assert before_restart["status"] == "completed"
    assert before_restart["run_id"] == SAVED_RUN_ID
    assert SAVED_RUN_ID in run_store.runs, "the replay result was committed"
    assert run_store.runs[SAVED_RUN_ID]["job_id"] == job_id, "the saved run links back to its job"
    assert run_store.jobs[job_id]["run_id"] == SAVED_RUN_ID, "the saved job links to its run"
    assert set(before_restart) == {
        "job_id", "submission_id", "status", "created_at", "started_at", "completed_at", "progress", "run_id", "error",
    }, "response shape: original keys plus the submission identity"
    scans_before_restart = len(scan_calls)

    # ── Process 2: the API restarts; the database keeps the saved run ─────
    _boot_fresh_api_process(monkeypatch, run_store)

    saved_run = server.get_enhanced_momentum_backtest(SAVED_RUN_ID)["run"]
    assert saved_run["id"] == SAVED_RUN_ID, "the result row survived the restart"
    assert saved_run["job_id"] == job_id, "the run detail exposes the job it came from"

    after_restart = _poll(job_id)

    assert after_restart["status"] == "completed" and after_restart["run_id"] == SAVED_RUN_ID, (
        f"After an API restart the caller polled job {job_id} and got {after_restart}; "
        f"the run it produced (#{SAVED_RUN_ID}) is still in the database."
    )
    assert after_restart["error"] is None
    assert after_restart["progress"]["percent"] == 100
    assert len(scan_calls) == scans_before_restart, "a completed job must not re-run the replay"


def test_replay_job_interrupted_by_api_restart_reports_a_terminal_state_not_404(monkeypatch):
    run_store = _FakeRunStore()
    _stub_replay_work(monkeypatch, run_store)
    monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", {})
    monkeypatch.setattr(server, "Thread", _NeverStartedThread)

    with freeze_time("2026-07-01 09:00:00") as clock:
        # ── Process 1: job is accepted and mid-flight when the process dies ─
        job_id = server.start_enhanced_momentum_backtest(_request())["job_id"]
        server._update_enhanced_backtest_job(
            job_id,
            status="running",
            started_at=datetime.now(timezone.utc),
            progress={
                "percent": 40,
                "phase": "Scanning cohort snapshot",
                "completed": 0,
                "total": 1,
                "unit": "snapshot",
                "detail": "Ranking Enhanced Momentum stocks",
            },
        )
        assert server.get_enhanced_momentum_backtest_job(job_id)["status"] == "running"
        assert not run_store.runs, "nothing was saved before the process died"

        # ── Process 2: fresh API; the worker thread did not come back with it ─
        _boot_fresh_api_process(monkeypatch, run_store)
        clock.tick(server._ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT + timedelta(seconds=1))

        after_restart = _poll(job_id)

    assert after_restart["status"] in {"failed", "interrupted"} and after_restart["run_id"] is None, (
        f"After an API restart the caller polled running job {job_id} and got {after_restart}. "
        "An unsaved job that died with the process must surface as a terminal failure; "
        "a 404 is indistinguishable from a job that never existed or one that finished and expired."
    )
    # Same response contract as any other failed job: a status and an error string.
    assert after_restart["status"] == "failed"
    assert "interrupted" in after_restart["error"]
    assert after_restart["completed_at"] is not None
    assert run_store.jobs[job_id]["status"] == "failed", "the settled outcome is durable too"


def test_job_running_in_another_api_process_is_reported_running_not_interrupted(monkeypatch):
    run_store = _FakeRunStore()
    _stub_replay_work(monkeypatch, run_store)
    monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", {})
    monkeypatch.setattr(server, "Thread", _NeverStartedThread)

    with freeze_time("2026-07-01 09:00:00") as clock:
        # ── Process 1 owns a live replay ──────────────────────────────────
        job_id = server.start_enhanced_momentum_backtest(_request())["job_id"]
        server._update_enhanced_backtest_job(
            job_id,
            status="running",
            started_at=datetime.now(timezone.utc),
            progress={"percent": 55, "phase": "Replaying", "completed": 5, "total": 9, "unit": "stocks", "detail": "..."},
        )

        # ── Process 2 comes up alongside it while the heartbeat is fresh ──
        _boot_fresh_api_process(monkeypatch, run_store)
        clock.tick(server._ENHANCED_BACKTEST_HEARTBEAT_INTERVAL)

        seen_elsewhere = _poll(job_id)
        assert seen_elsewhere["status"] == "running", seen_elsewhere
        assert seen_elsewhere["progress"]["percent"] == 55
        assert seen_elsewhere["error"] is None
        assert run_store.jobs[job_id]["status"] == "running", "reading from another process must not settle a live job"

        # The single-replay guard also sees the job running elsewhere.
        try:
            server.start_enhanced_momentum_backtest(_request())
        except HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("a second replay must be refused while one is running elsewhere")

        # ── Only once process 1 stops heartbeating is the job settled ─────
        clock.tick(server._ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT)
        settled = _poll(job_id)

    assert settled["status"] == "failed"
    assert "interrupted" in settled["error"]
    assert run_store.jobs[job_id]["status"] == "failed"


def test_job_whose_final_status_write_was_lost_is_recovered_from_the_saved_run(monkeypatch):
    """The run row carries the job id, so a lost terminal write is not a lost job."""
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    monkeypatch.setattr(server, "_enhanced_momentum_backtest_jobs", {})
    monkeypatch.setattr(server, "Thread", _InlineThread)

    real_execute = run_store.execute
    outage = [True]

    def flaky_execute(sql, params):
        # The database goes away exactly when the worker reports completion,
        # and is back by the time the next process looks.
        if outage[0] and "UPDATE" in sql and params.get("status") == "completed":
            outage[0] = False
            raise DatabaseError("connection lost")
        return real_execute(sql, params)

    monkeypatch.setattr(run_store, "execute", flaky_execute)

    with freeze_time("2026-07-01 09:00:00") as clock:
        job_id = server.start_enhanced_momentum_backtest(_request())["job_id"]
        # Process 1 still knows the truth from memory ...
        assert server.get_enhanced_momentum_backtest_job(job_id)["status"] == "completed"
        # ... but storage never heard about it.
        assert run_store.jobs[job_id]["status"] == "running"
        assert run_store.runs[SAVED_RUN_ID]["job_id"] == job_id

        _boot_fresh_api_process(monkeypatch, run_store)
        clock.tick(server._ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT + timedelta(seconds=1))
        recovered = _poll(job_id)

    assert recovered["status"] == "completed"
    assert recovered["run_id"] == SAVED_RUN_ID
    assert recovered["error"] is None
    assert run_store.jobs[job_id]["run_id"] == SAVED_RUN_ID
    assert len(scan_calls) == 1, "recovery reads the saved run; it never re-runs the replay"


def test_simultaneous_submissions_from_two_processes_admit_exactly_one(monkeypatch):
    """Both processes read an empty active list; storage must admit only one."""
    run_store = _FakeRunStore()
    _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _NeverStartedThread)
    process_b = _ApiProcess("b", _NeverStartedThread)

    # Neither submission has landed when each process checks for an active
    # replay, exactly as two read-committed reads would see it.
    with run_store.stale_reads():
        process_a.activate(monkeypatch, run_store)
        admitted = server.start_enhanced_momentum_backtest(_request())

        process_b.activate(monkeypatch, run_store)
        with pytest.raises(HTTPException) as refused:
            server.start_enhanced_momentum_backtest(_request())

    assert refused.value.status_code == 409
    active = [row for row in run_store.jobs.values() if row["status"] in _FakeRunStore.ACTIVE]
    assert [row["job_id"] for row in active] == [admitted["job_id"]], "storage admitted exactly one replay"
    assert active[0]["worker_id"] == process_a.worker_id
    assert admitted["job_id"] in process_a.jobs and not process_b.jobs


def test_expired_worker_cannot_write_after_its_job_was_settled_and_replaced(monkeypatch):
    """A starved worker that wakes up late must not save progress, status or a result."""
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _InlineThread)
    process_b = _ApiProcess("b", _NeverStartedThread)
    clock_holder = {}
    seen_by_b = {}

    real_scan = server._enhanced_momentum_scan_session_worker

    def starved_scan(session_date, filters):
        # Process A's worker is stalled inside the replay for longer than the
        # heartbeat timeout. Meanwhile process B settles A's job and admits a
        # replacement. Then A's worker resumes as if nothing happened.
        clock_holder["clock"].tick(server._ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT + timedelta(seconds=1))
        process_b.activate(monkeypatch, run_store)
        seen_by_b["settled"] = _poll(job_holder["a"])
        seen_by_b["replacement"] = server.start_enhanced_momentum_backtest(_request())
        process_a.activate(monkeypatch, run_store)
        return real_scan(session_date, filters)

    monkeypatch.setattr(server, "_enhanced_momentum_scan_session_worker", starved_scan)

    job_holder = {}
    with freeze_time("2026-07-01 09:00:00") as clock:
        clock_holder["clock"] = clock
        process_a.activate(monkeypatch, run_store)
        # ``start`` returns the queued snapshot; with the inline thread the
        # whole worker (and the stall above) has run by the time it returns.
        job_holder["a"] = None
        original_start = server._run_enhanced_momentum_backtest_job

        def capture_then_run(job_id, request):
            job_holder["a"] = job_id
            original_start(job_id, request)

        monkeypatch.setattr(server, "_run_enhanced_momentum_backtest_job", capture_then_run)
        server.start_enhanced_momentum_backtest(_request())
        a_job = job_holder["a"]
        replacement = seen_by_b["replacement"]["job_id"]

        # ── What process B did while A was stalled ────────────────────────
        assert seen_by_b["settled"]["status"] == "failed"
        assert "interrupted" in seen_by_b["settled"]["error"]
        assert replacement != a_job

        # ── A's late writes must all have been refused ────────────────────
        a_row = run_store.jobs[a_job]
        assert a_row["status"] == "failed", f"late worker overwrote settled job: {a_row['status']}"
        assert a_row["run_id"] is None
        assert "interrupted" in a_row["error"]
        assert [run for run in run_store.runs.values() if run["job_id"] == a_job] == [], "no late result saved"
        assert len(scan_calls) == 1

        # It was the storage fence that stopped the late worker, not luck:
        # its result save was rejected, and nothing it wrote landed after the
        # settlement.
        writes = run_store.job_writes
        settled_at = next(i for i, (kind, params) in enumerate(writes)
                          if kind == "update" and params["job_id"] == a_job and params["status"] == "failed")
        late_a_writes = [(kind, params) for kind, params in writes[settled_at + 1:] if params["job_id"] == a_job]
        assert ("run-save-rejected", {"job_id": a_job, "owner_id": process_a.worker_id}) in late_a_writes
        assert all(kind.endswith("-rejected") for kind, _ in late_a_writes), late_a_writes

        # The replacement is untouched and still owned by process B.
        b_row = run_store.jobs[replacement]
        assert b_row["status"] == "queued" and b_row["worker_id"] == process_b.worker_id

        # Process A no longer claims the job: it answers from storage like anyone else.
        process_a.activate(monkeypatch, run_store)
        assert _poll(a_job)["status"] == "failed"

        # Even if A's worker still held the job in memory, the storage fence
        # refuses its heartbeat, progress and terminal writes.
        process_a.jobs[a_job] = {
            "job_id": a_job, "status": "running", "created_at": datetime.now(timezone.utc),
            "started_at": None, "completed_at": None, "run_id": None, "error": None,
            "progress": {"percent": 70, "phase": "late", "completed": 0, "total": 0, "unit": "x", "detail": ""},
        }
        before = dict(run_store.jobs[a_job])
        server._flush_enhanced_backtest_job_heartbeat(a_job)
        assert run_store.jobs[a_job] == before, "late heartbeat/progress must not land"
        with pytest.raises(server._EnhancedBacktestOwnershipLost):
            server._update_enhanced_backtest_job(a_job, status="completed", run_id=999)
        assert run_store.jobs[a_job] == before, "late terminal write must not land"
        assert a_job not in process_a.jobs, "a fenced-out worker forfeits the job"


# ── Caller-supplied submission identity ───────────────────────────────────


def test_retrying_a_submission_returns_the_original_job_and_outcome_across_processes_and_restarts(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)
    identity = "ui-7f3c0d1e"

    # The caller's first response is lost on the wire; the job ran anyway.
    first = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert first["submission_id"] == identity

    # Same process, same identity: the original job, with its outcome.
    retried = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert retried["job_id"] == first["job_id"]
    assert retried["status"] == "completed" and retried["run_id"] == SAVED_RUN_ID

    # Different process, same identity: same answer from storage.
    _ApiProcess("b", _NeverStartedThread).activate(monkeypatch, run_store)
    from_b = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert from_b["job_id"] == first["job_id"] and from_b["run_id"] == SAVED_RUN_ID

    # After a restart, still the same job; nothing was re-run anywhere.
    _boot_fresh_api_process(monkeypatch, run_store)
    after_restart = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert after_restart["job_id"] == first["job_id"] and after_restart["status"] == "completed"
    assert len(run_store.jobs) == 1 and len(run_store.runs) == 1
    assert len(scan_calls) == 1, "retries must not start another replay"
    assert run_store.jobs[first["job_id"]]["submission_id"] == identity, "identity is persisted with the job"


def test_identical_parameters_with_a_new_identity_start_a_deliberate_new_run(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)

    first = server.start_enhanced_momentum_backtest(_request(submission_id="run-one"))
    second = server.start_enhanced_momentum_backtest(_request(submission_id="run-two"))

    assert second["job_id"] != first["job_id"]
    outcomes = {_poll(first["job_id"])["run_id"], _poll(second["job_id"])["run_id"]}
    assert outcomes == {SAVED_RUN_ID, SAVED_RUN_ID + 1}
    assert len(scan_calls) == 2
    assert len(run_store.jobs) == 2 and len(run_store.runs) == 2


def test_reusing_an_identity_with_different_parameters_is_rejected(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)

    first = server.start_enhanced_momentum_backtest(_request(submission_id="ui-1", top_n=30))
    with pytest.raises(HTTPException) as rejected:
        server.start_enhanced_momentum_backtest(_request(submission_id="ui-1", top_n=50))

    assert rejected.value.status_code == 422
    assert "ui-1" in rejected.value.detail and "different" in rejected.value.detail
    assert len(run_store.jobs) == 1 and len(scan_calls) == 1
    # The original is untouched and still answers to its identity.
    again = server.start_enhanced_momentum_backtest(_request(submission_id="ui-1", top_n=30))
    assert again["job_id"] == first["job_id"]


def test_concurrent_retries_of_one_submission_converge_on_one_job(monkeypatch):
    """Two processes retry the same identity before either submission lands."""
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _NeverStartedThread)
    process_b = _ApiProcess("b", _NeverStartedThread)
    identity = "ui-retry-9"

    with run_store.stale_reads():
        process_a.activate(monkeypatch, run_store)
        from_a = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
        process_b.activate(monkeypatch, run_store)
        from_b = server.start_enhanced_momentum_backtest(_request(submission_id=identity))

    assert from_b["job_id"] == from_a["job_id"], "a retry that lost the race gets the winner's job, not a 409"
    assert from_b["status"] in {"queued", "running"} and from_b["submission_id"] == identity
    assert len(run_store.jobs) == 1 and len(scan_calls) == 0
    assert run_store.jobs[from_a["job_id"]]["worker_id"] == process_a.worker_id
    assert from_a["job_id"] in process_a.jobs and not process_b.jobs


def test_committed_submission_whose_response_was_lost_is_recovered_by_retrying(monkeypatch):
    """Admission commits, the reply never arrives, and the worker was never started."""
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)
    identity = "ui-lost-reply"

    run_store.lose_next_admission_response = True
    with pytest.raises(HTTPException) as lost:
        server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert lost.value.status_code == 503
    committed = [row for row in run_store.jobs.values() if row["submission_id"] == identity]
    assert len(committed) == 1 and committed[0]["status"] == "queued" and len(scan_calls) == 0

    # The caller retries with the same identity: the committed job is picked
    # up and run - once - rather than being failed or duplicated.
    retried = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert retried["job_id"] == committed[0]["job_id"]
    outcome = _poll(retried["job_id"])
    assert outcome["status"] == "completed" and outcome["run_id"] == SAVED_RUN_ID, outcome
    assert len(run_store.jobs) == 1 and len(run_store.runs) == 1 and len(scan_calls) == 1


def test_repeated_result_save_is_idempotent_and_does_not_recompute(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)

    # The result insert commits but its reply is lost; the worker must end up
    # with exactly that run, not a failed job or a duplicate run.
    run_store.lose_next_run_save_response = True
    job = _poll(server.start_enhanced_momentum_backtest(_request(submission_id="ui-save-once"))["job_id"])

    assert job["status"] == "completed" and job["run_id"] == SAVED_RUN_ID, job
    assert list(run_store.runs) == [SAVED_RUN_ID]
    assert len(scan_calls) == 1, "the replay was computed once"
    saved_summary = json.loads(run_store.runs[SAVED_RUN_ID]["summary_json"])

    # Saving the same job's result again is a no-op that returns the saved run.
    again = server._save_enhanced_backtest_run(
        run_store,
        tuple(run_store.inserted_params[0][:-1]),
        job_id=job["job_id"],
    )
    assert again["id"] == SAVED_RUN_ID
    assert list(run_store.runs) == [SAVED_RUN_ID]
    assert json.loads(run_store.runs[SAVED_RUN_ID]["summary_json"]) == saved_summary


def test_concurrent_retry_after_the_original_finished_hits_the_identity_index_and_converges(monkeypatch):
    """Slot free, identity already committed: the unique index, not the
    active-slot guard, is what stops the duplicate."""
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    identity = "ui-fast-finish"

    with run_store.stale_reads():
        # Process B's lookup ran before A's submission landed ...
        _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)
        from_a = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
        assert _poll(from_a["job_id"])["status"] == "completed"
        # ... and by the time B admits, A's job is already finished.
        _ApiProcess("b", _NeverStartedThread).activate(monkeypatch, run_store)
        from_b = server.start_enhanced_momentum_backtest(_request(submission_id=identity))

    assert from_b["job_id"] == from_a["job_id"]
    assert from_b["status"] == "completed" and from_b["run_id"] == SAVED_RUN_ID
    assert len(run_store.jobs) == 1 and len(run_store.runs) == 1 and len(scan_calls) == 1
    kinds = [kind for kind, params in run_store.job_writes if params.get("submission_id") == identity]
    assert kinds.count("insert") == 1 and "admission-duplicate" in kinds, kinds


# ── Retry that loses the lookup race to a concurrent admission ─────────────


def test_retry_that_lost_the_lookup_race_in_the_same_process_gets_the_original_job_not_409(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    _ApiProcess("a", _NeverStartedThread).activate(monkeypatch, run_store)
    identity = "ui-double-click"

    first = server.start_enhanced_momentum_backtest(_request(submission_id=identity))

    # The retry's identity lookup ran before the first call's admission
    # committed, so it finds nothing and walks into the in-process busy guard.
    run_store.race_identity_lookups(identity, misses=1)
    retried = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert retried["job_id"] == first["job_id"], retried
    assert retried["status"] == "queued" and retried["submission_id"] == identity
    assert len(run_store.jobs) == 1 and len(scan_calls) == 0

    # Matching identity but different parameters is still a 422, not a 409.
    run_store.race_identity_lookups(identity, misses=1)
    with pytest.raises(HTTPException) as mismatch:
        server.start_enhanced_momentum_backtest(_request(submission_id=identity, top_n=50))
    assert mismatch.value.status_code == 422

    # Genuinely different submissions are still refused while the slot is taken.
    for other in ("ui-someone-else", None):
        with pytest.raises(HTTPException) as busy:
            server.start_enhanced_momentum_backtest(_request(submission_id=other))
        assert busy.value.status_code == 409, other
    assert len(run_store.jobs) == 1


def test_retry_that_lost_the_lookup_race_across_processes_gets_the_original_job_not_409(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _NeverStartedThread)
    process_b = _ApiProcess("b", _NeverStartedThread)
    identity = "ui-two-workers"

    process_a.activate(monkeypatch, run_store)
    first = server.start_enhanced_momentum_backtest(_request(submission_id=identity))

    # Process B's retry looked the identity up before A's admission landed,
    # then hits the cross-process busy guard (A's job is alive in storage).
    process_b.activate(monkeypatch, run_store)
    run_store.race_identity_lookups(identity, misses=1)
    from_b = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert from_b["job_id"] == first["job_id"], from_b
    assert from_b["status"] == "queued" and from_b["submission_id"] == identity
    assert run_store.jobs[first["job_id"]]["status"] == "queued", "reading it from B must not settle it"
    assert run_store.jobs[first["job_id"]]["worker_id"] == process_a.worker_id
    assert not process_b.jobs and len(run_store.jobs) == 1 and len(scan_calls) == 0

    run_store.race_identity_lookups(identity, misses=1)
    with pytest.raises(HTTPException) as mismatch:
        server.start_enhanced_momentum_backtest(_request(submission_id=identity, top_n=50))
    assert mismatch.value.status_code == 422

    for other in ("ui-someone-else", None):
        with pytest.raises(HTTPException) as busy:
            server.start_enhanced_momentum_backtest(_request(submission_id=other))
        assert busy.value.status_code == 409, other


# ── Recovery: committed result behind repeated transport errors ────────────


def test_result_that_committed_behind_repeated_transport_errors_is_reconciled_not_failed(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)
    identity = "ui-flaky-link"

    # The result insert commits but its reply is lost, and the one retry of
    # the save dies on the wire before it is even sent.
    run_store.lose_next_run_save_response = True
    run_store.fail_next_run_saves = 1
    job_id = server.start_enhanced_momentum_backtest(_request(submission_id=identity))["job_id"]

    assert list(run_store.runs) == [SAVED_RUN_ID] and run_store.runs[SAVED_RUN_ID]["job_id"] == job_id
    outcome = _poll(job_id)
    assert outcome["status"] == "completed" and outcome["run_id"] == SAVED_RUN_ID, (
        f"the job was finalised as {outcome['status']!r} although its result is saved: {outcome.get('error')}"
    )
    stored = run_store.jobs[job_id]
    assert stored["status"] == "completed" and stored["run_id"] == SAVED_RUN_ID and stored["error"] is None

    # Retrying the original submission recovers that result; nothing is recalculated.
    retried = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert retried["job_id"] == job_id and retried["status"] == "completed" and retried["run_id"] == SAVED_RUN_ID
    _boot_fresh_api_process(monkeypatch, run_store)
    after_restart = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert after_restart["run_id"] == SAVED_RUN_ID
    assert len(scan_calls) == 1 and len(run_store.runs) == 1


def test_unverifiable_failure_is_left_for_settlement_from_saved_evidence(monkeypatch):
    """If even the evidence check cannot reach storage, the worker must not
    assert an outcome it cannot know; storage settles it later from the run."""
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _InlineThread).activate(monkeypatch, run_store)

    run_store.lose_next_run_save_response = True
    run_store.fail_next_run_saves = 1
    run_store.fail_next_run_lookups = 1
    job_id = server.start_enhanced_momentum_backtest(_request(submission_id="ui-dark-link"))["job_id"]

    assert run_store.jobs[job_id]["status"] == "running", "no outcome was asserted without evidence"
    assert job_id not in process_a.jobs, "the worker no longer vouches for the job from memory"
    # Storage is reachable again: the first reader settles it from the saved run.
    outcome = _poll(job_id)
    assert outcome["status"] == "completed" and outcome["run_id"] == SAVED_RUN_ID, outcome
    assert run_store.jobs[job_id]["status"] == "completed"
    assert len(scan_calls) == 1 and len(run_store.runs) == 1


# ── Recovery: the worker thread cannot be started after admission ──────────


def test_worker_launch_failure_reports_a_durable_outcome_and_frees_the_slot(monkeypatch):
    run_store = _FakeRunStore()
    scan_calls = _stub_replay_work(monkeypatch, run_store)
    process_a = _ApiProcess("a", _UnstartableThread).activate(monkeypatch, run_store)
    _UnstartableThread.attempts = 0
    identity = "ui-no-threads"

    try:
        response = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    except Exception as error:  # noqa: BLE001 - the pre-fix behaviour leaks the thread error
        response = {"status": f"raised {type(error).__name__}", "error": str(error)}

    stored = [row for row in run_store.jobs.values() if row["submission_id"] == identity]
    assert len(stored) == 1
    job_id = stored[0]["job_id"]
    assert response["status"] == "failed" and "could not be started" in (response["error"] or ""), response
    assert stored[0]["status"] == "failed" and "could not be started" in stored[0]["error"], stored[0]
    assert not any(job["status"] in _FakeRunStore.ACTIVE for job in process_a.jobs.values()), (
        "an unlaunched job must not stay queued in the local registry"
    )
    assert _UnstartableThread.attempts == 1

    # Retrying the identity reports the same outcome - and starts nothing.
    retried = server.start_enhanced_momentum_backtest(_request(submission_id=identity))
    assert retried["job_id"] == job_id and retried["status"] == "failed"
    assert _UnstartableThread.attempts == 1 and len(scan_calls) == 0
    with pytest.raises(HTTPException) as mismatch:
        server.start_enhanced_momentum_backtest(_request(submission_id=identity, top_n=50))
    assert mismatch.value.status_code == 422

    # The slot is free: a new submission (threads available again) runs normally.
    process_a.thread_cls = _InlineThread
    process_a.activate(monkeypatch, run_store)
    fresh = server.start_enhanced_momentum_backtest(_request(submission_id="ui-second-try"))
    assert fresh["job_id"] != job_id
    assert _poll(fresh["job_id"])["status"] == "completed" and len(scan_calls) == 1
