"""Entry points for genuinely separate API processes in the replay job tests.

Each child is started with the ``spawn`` context, so it imports ``api.server``
afresh: its own job registry, lock and worker identity - exactly what a
second uvicorn worker or a restarted API has. The only things shared are a
disposable PostgreSQL database and the synchronisation primitives handed in
from the parent (barriers / events), which make the interleavings repeatable.

Only the Enhanced Momentum *scanner* is stubbed. Admission, ownership fences,
heartbeats, settlement and the fenced, idempotent result save all run the real
code against the real database.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

SNAPSHOT_DATE = date(2026, 6, 29)
END_DATE = date(2026, 6, 30)

# Set by the entry point before the scanner stub can be called.
_STALL_ENTERED = None   # multiprocessing.Event: the worker is inside the replay
_STALL_RESUME = None    # multiprocessing.Event: let the worker continue
_RESUME_AFTER_FORFEIT = False  # on resume, wait until the heartbeat has forfeited the job
_SERVER = None
_SAVE_LOG: list[str] = []


class _NullConfigStore:
    def get(self, _key, default=None):
        return default


def _scan_session(session_date, _filters):
    """Stand-in for the Enhanced Momentum scan: three ranked candidates."""
    if _STALL_ENTERED is not None:
        _STALL_ENTERED.set()
        _STALL_RESUME.wait(120)
        if _RESUME_AFTER_FORFEIT:
            # Let the heartbeat thread be the one that discovers the lost
            # ownership: only continue once it has dropped the job.
            deadline = time.monotonic() + 30
            while _SERVER._enhanced_momentum_backtest_jobs and time.monotonic() < deadline:
                time.sleep(0.02)
    return {
        "matching_stocks": 3,
        "returned_stocks": 3,
        "market_context": {"regime": "neutral"},
        "formula": {},
        "results": [
            {
                "stock_id": stock_id,
                "symbol": f"STOCK{stock_id}",
                "name": f"Stock {stock_id}",
                "company_name": f"Stock {stock_id} Ltd",
                "industry": "Industrials",
                "as_of_date": session_date.isoformat(),
                "rank": stock_id,
                "enhanced_score": 100.0 - stock_id,
            }
            for stock_id in (1, 2, 3)
        ],
    }


def configure_process(dsn: dict[str, Any], heartbeat_interval: float, heartbeat_timeout: float):
    """Point this process's ``api.server`` at the scratch database."""
    from api import server
    from storage.database import Database

    db = Database(**dsn)
    db.connect()
    server.get_db = lambda: db
    server.store = lambda _name: _NullConfigStore()
    server._enhanced_momentum_scan_session_worker = _scan_session
    server._ENHANCED_BACKTEST_HEARTBEAT_INTERVAL = timedelta(seconds=heartbeat_interval)
    server._ENHANCED_BACKTEST_HEARTBEAT_TIMEOUT = timedelta(seconds=heartbeat_timeout)

    real_save = server._save_enhanced_backtest_run

    def recording_save(db_, values, *, job_id):
        try:
            row = real_save(db_, values, job_id=job_id)
        except server._EnhancedBacktestOwnershipLost:
            _SAVE_LOG.append("save-rejected-ownership-lost")
            raise
        _SAVE_LOG.append(f"saved-run-{row['id']}")
        return row

    server._save_enhanced_backtest_run = recording_save
    return server, db


def request_for(server, identity: str | None, top_n: int = 30):
    return server.EnhancedMomentumBacktestRequest(
        submission_id=identity,
        start_date=SNAPSHOT_DATE,
        end_date=END_DATE,
        mode="buy_and_hold",
        top_n=top_n,
        capital_per_stock=100_000,
        filters=server.EnhancedMomentumScanRequest(max_results=1),
    )


def _wait_until_terminal(server, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = server.get_enhanced_momentum_backtest_job(job_id)
        if snapshot["status"] not in server._ENHANCED_BACKTEST_ACTIVE_STATUSES or time.monotonic() > deadline:
            return snapshot
        time.sleep(0.05)


def submit(
    name: str,
    dsn: dict[str, Any],
    identity: str | None,
    results,
    *,
    barrier=None,
    admission_done=None,
    stall_entered=None,
    stall_resume=None,
    heartbeat_interval: float = 0.5,
    heartbeat_timeout: float = 2.0,
    resume_after_forfeit: bool = False,
    top_n: int = 30,
) -> None:
    """Submit one replay from this process and report what happened.

    With a ``barrier`` every participant submits at the same instant; with
    ``admission_done`` every participant (and the parent) rendezvous after
    its admission attempt has returned, whatever the result. With the stall
    events the worker parks inside the replay until released (the parent may
    freeze the whole process meanwhile). The process waits for a job it owns
    to finish before reporting, so the report carries the outcome this
    worker believes in.
    """
    global _STALL_ENTERED, _STALL_RESUME, _RESUME_AFTER_FORFEIT, _SERVER
    from fastapi import HTTPException

    _STALL_ENTERED, _STALL_RESUME, _RESUME_AFTER_FORFEIT = stall_entered, stall_resume, resume_after_forfeit
    server, db = configure_process(dsn, heartbeat_interval, heartbeat_timeout)
    _SERVER = server
    report: dict[str, Any] = {"process": name, "worker_id": server._ENHANCED_BACKTEST_WORKER_ID}
    try:
        request = request_for(server, identity, top_n)
        if barrier is not None:
            barrier.wait(30)
        try:
            response = server.start_enhanced_momentum_backtest(request)
        except HTTPException as error:
            report.update({"refused": error.status_code, "detail": error.detail})
            if admission_done is not None:
                admission_done.wait(30)
            return
        if admission_done is not None:
            admission_done.wait(30)
        owned = response["job_id"] in server._enhanced_momentum_backtest_jobs
        report.update({"response": response, "owned": owned})
        if owned:
            report["final"] = _wait_until_terminal(server, response["job_id"])
        else:
            report["final"] = server.get_enhanced_momentum_backtest_job(response["job_id"])
        report["registry"] = {
            job_id: job["status"] for job_id, job in server._enhanced_momentum_backtest_jobs.items()
        }
        report["save_log"] = list(_SAVE_LOG)
    except Exception as error:  # noqa: BLE001 - surface anything unexpected to the parent
        report["crashed"] = f"{type(error).__name__}: {error}"
    finally:
        results.put(report)
        try:
            db.disconnect()
        except Exception:  # noqa: BLE001
            pass
