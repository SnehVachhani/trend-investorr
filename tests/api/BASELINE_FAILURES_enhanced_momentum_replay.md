# Baseline failures: tests/api/test_enhanced_momentum_replay_workers.py

Recorded before any new test or production change was made.

Command:

    /tmp/venv/bin/python -m pytest -q tests/api/test_enhanced_momentum_replay_workers.py

Result: 2 failed, 8 passed.

Both failures are pre-existing and are documented in README.md as known
baseline failures. Neither is related to backtest job tracking or to the
"lost job" regression investigated separately. They are NOT to be counted
against the new regression test, and they were not touched.

## 1. test_replay_refreshes_ranked_cohorts_and_runs_setup_filters_on_active_members

    AttributeError: <module 'api.server'> has no attribute
    '_enhanced_backtest_profile_stock_batch'

Cause: the test monkeypatches a helper that no longer exists. The per-stock
overlay batch is now `_enhanced_backtest_selected_stock_batch`
(api/server.py:3524, submitted to the overlay pool at api/server.py:4489).
The test needs to be re-pointed at the new helper (and its return shape
checked) before it can say anything about replay behaviour.

## 2. test_backtest_loader_reads_only_warmup_and_requested_window

    AssertionError at index 1:
      datetime(2026, 6, 26, 0, 0, tzinfo=ZoneInfo('Asia/Kolkata'))
      != datetime(2026, 6, 26, 0, 0, tzinfo=timezone.utc)

Cause: the test asserts UTC query boundaries, but `_backtest_date_bounds`
(api/server.py:6741) deliberately interprets date-only inputs as NSE session
dates in Asia/Kolkata so the 15:30 IST daily close stays inside the requested
day. The production behaviour is the intended one; the expectation in the
test is stale.

## After the persistent job-tracking change

Re-run of the same file: still exactly these 2 failures, 8 passed. Both are
unchanged and remain outside the scope of the job-tracking work.

One previously-passing test in this file was edited, not to change its
expectations but because the contract changed underneath it:
`test_replay_job_returns_pollable_progress_handle` now patches `get_db` with
a tiny in-memory jobs table, since a replay job is saved to storage before
its worker thread is started.

Full-suite check: `pytest tests/` reports the same 19 pre-existing failures
on the original tree and on the changed tree (564 -> 568 passed; the +4 are
the new durability tests). None of the 19 involve the replay job endpoints.

## After atomic admission and owner-fenced writes

`tests/api/test_enhanced_momentum_replay_workers.py`: still exactly the same
2 baseline failures, 8 passed. The `_JobTable` fake inside
`test_replay_job_returns_pollable_progress_handle` gained an
`execute_returning` for the admission statement; its expectations are unchanged.

New regressions in `test_enhanced_momentum_backtest_job_durability.py`,
recorded failing BEFORE the fix:

    test_simultaneous_submissions_from_two_processes_admit_exactly_one
        Failed: DID NOT RAISE HTTPException     (second process was admitted)
    test_expired_worker_cannot_write_after_its_job_was_settled_and_replaced
        AssertionError: late worker overwrote settled job: completed

After the fix: 6 passed in that file. Full suite: 19 failed / 570 passed,
the 19 identical to the original tree.

## After caller-supplied submission identity (idempotent submissions)

`tests/api/test_enhanced_momentum_replay_workers.py`: still exactly the same
2 baseline failures, 8 passed. Not edited in this step.

New regressions in `test_enhanced_momentum_backtest_job_durability.py`,
recorded failing BEFORE the fix (the request model silently ignored
`submission_id`, so every failure is behavioural):

    test_retrying_a_submission_returns_the_original_job_and_outcome_...
        KeyError: 'submission_id'                 (identity not persisted/returned)
    test_reusing_an_identity_with_different_parameters_is_rejected
        Failed: DID NOT RAISE HTTPException       (second replay started)
    test_concurrent_retries_of_one_submission_converge_on_one_job
        HTTPException: 409 ...already running     (retry treated as a new job)
    test_committed_submission_whose_response_was_lost_is_recovered_by_retrying
        KeyError: 'submission_id'
    test_repeated_result_save_is_idempotent_and_does_not_recompute
        assert 'failed' == 'completed'            (lost save reply failed the job)

`test_identical_parameters_with_a_new_identity_start_a_deliberate_new_run`
passed before and after; it guards the other direction.

After the fix: 13 passed in that file. Full suite: 19 failed / 577 passed,
the 19 identical to the original tree.

## After the pre-admission retry race fix and the result-index upgrade migration

`tests/api/test_enhanced_momentum_replay_workers.py`: still exactly the same
2 baseline failures, 8 passed. Not edited in this step.

Recorded failing BEFORE the fix:

    durability: test_retry_that_lost_the_lookup_race_in_the_same_process_gets_the_original_job_not_409
        HTTPException: 409 ... already running
    durability: test_retry_that_lost_the_lookup_race_across_processes_gets_the_original_job_not_409
        HTTPException: 409 ... already running
    postgres:   test_result_index_upgrade_keeps_every_saved_result_and_relinks_one_per_job
        AssertionError: CREATE INDEX idx_enhanced_momentum_backtests_job ON ... (job_id)
        (re-running migrations on a database with the early non-unique index
         left it non-unique: CREATE UNIQUE INDEX IF NOT EXISTS is a no-op)

The PostgreSQL-backed file runs against a scratch server when
ALGOANALYST_TEST_PG_HOST (and optionally _PORT/_USER/_PASSWORD) is set; it
skips otherwise. It was run here against a throwaway PostgreSQL 16 cluster.

After the fix: durability 15 passed; postgres 2 passed (2 skipped without a
server). Full suite with the server: 19 failed / 581 passed, the 19 identical
to the original tree.

## After the two recovery paths (reconcile-before-failure, worker launch failure)

`tests/api/test_enhanced_momentum_replay_workers.py`: still exactly the same
2 baseline failures, 8 passed. Not edited in this step.

Recorded failing BEFORE the fix (durability file):

    test_result_that_committed_behind_repeated_transport_errors_is_reconciled_not_failed
        the job was finalised as 'failed' although its result is saved: connection reset after commit
    test_unverifiable_failure_is_left_for_settlement_from_saved_evidence
        no outcome was asserted without evidence: assert 'failed' == 'running'
    test_worker_launch_failure_reports_a_durable_outcome_and_frees_the_slot
        {'status': 'raised RuntimeError', 'error': "can't start new thread"}

After the fix: durability 18 passed; postgres-backed 2 passed (against a
throwaway PostgreSQL 16). Full suite with the PostgreSQL checks enabled:
19 failed / 584 passed, the 19 identical to the original tree.

## Multi-process verification on a shared disposable PostgreSQL

`tests/api/test_enhanced_momentum_replay_workers.py`: still exactly the same
2 baseline failures, 8 passed. Not edited in this step.

New in `test_enhanced_momentum_backtest_job_postgres.py` (spawned processes
via `tests/api/_replay_process_harness.py`, real PostgreSQL 16):

    test_two_processes_submitting_different_replays_at_once_admit_exactly_one
    test_two_processes_retrying_one_submission_at_once_converge_on_one_job
    test_expired_worker_resuming_after_replacement_cannot_publish[heartbeat]
    test_expired_worker_resuming_after_replacement_cannot_publish[result-save]

What the checks exposed: no production defect. One test expectation was
non-deterministic - after the frozen worker is thawed, its heartbeat thread
and its worker thread race to be the first fenced write (the outcome is
identical either way). That variant now synchronises on the heartbeat's
forfeiture instead of assuming timing. Six consecutive loops: all green.

The PostgreSQL log confirmed the arbitration happened in the database:
unique-violations on idx_enhanced_momentum_backtest_jobs_single_active and
idx_enhanced_momentum_backtest_jobs_submission were raised against the
losing processes; no other server errors.

Results: postgres-backed 6 passed; durability 18 passed; full suite with the
PostgreSQL checks enabled 19 failed / 588 passed, the 19 identical to the
original tree.

## Handoff: baseline tests updated, acceptance selection green

The two README baseline failures in `test_enhanced_momentum_replay_workers.py`
were updated to the current contract; no production code changed for them:

* `test_replay_refreshes_ranked_cohorts_and_runs_setup_filters_on_active_members`
  now patches `_enhanced_backtest_selected_stock_batch` (the replacement for the
  removed `_enhanced_backtest_profile_stock_batch`), reads the entry-gate map
  from its new position (second-to-last argument, before `fundamental_setup_ids`),
  and checks the renamed progress phase "Evaluating selected replay setups".
  Every behavioural assertion (cohort refresh, stale exclusion, strategy order,
  pool sizing, progress) is unchanged.
* `test_backtest_loader_reads_only_warmup_and_requested_window` expects
  Asia/Kolkata boundaries (the intended behaviour of `_backtest_date_bounds`)
  and pins "today" with freezegun so the loader's in-progress-day clamp can
  never make the test date-dependent. The warmup/window assertion is unchanged.

Acceptance selection (scratch PostgreSQL via scripts/scratch_postgres.sh):
34 passed (10 + 18 + 6), repeated x3. Without a server the 6 PostgreSQL
tests skip. Full suite with PostgreSQL enabled: 17 failed / 590 passed; the
17 are all pre-existing failures from the original tree (the original 19
minus the two updated above), none in the replay/job files.
