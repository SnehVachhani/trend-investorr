# Task A, Check 8 completion review

Reviewed the final cumulative export `55614414-1b70-451b-a93a-2b246322227c_8_a_changed_files` against the shared `AlgoAnalystBackend.zip` starter, reconstructed in a separate temporary workspace.

## Independently verified acceptance

```sh
python -m pytest -q tests/api/test_enhanced_momentum_replay_workers.py tests/api/test_enhanced_momentum_backtest_job_durability.py tests/api/test_enhanced_momentum_backtest_job_postgres.py
```

**34 passed in 14.37s; no failures, collection errors or skipped tests.** Breakdown: 10 existing replay checks, 18 durability/recovery checks, six actual PostgreSQL checks including spawned-process scenarios.

Used the export's `scripts/scratch_postgres.sh` to create and stop a disposable PostgreSQL 14.23 cluster, with a unique temporary data/socket directory and port 55439. Invoked the script with `sh` so extraction permissions do not affect execution; the patch itself declares executable mode. Set the test connection variables explicitly. No production database was used. The scratch cluster was stopped and deleted after testing. Dependencies were supplied by the isolated Python 3.12 environment installed from the common starter manifests, which this export does not change; this review does not claim a second cold dependency install.

## Source and test review

The race checks now have a three-party barrier after admission and park the winner's replay until the parent observes the sole running job. This genuinely forces admission overlap. Stale-owner tests retain their heartbeat and result-save variants.

The earlier failing replay tests have legitimate fixture/contract corrections: the renamed selected-stock helper, the entry-signal map's current argument position, the current phase label, IST session boundaries, and a fixed clock keeping the requested window historical. Their cohort/strategy/pool and warm-up behavior assertions remain. No production calculation code was changed in this final step; runtime API/storage files are unchanged from Check 6.

## Completion assessment

The final export meets Task A's agreed backend criteria under the verified acceptance selection. No additional coding prompt is indicated. No new behavior flag is supported. The screenshot reports 17 failures elsewhere in the full repository; that broader run was not independently repeated here, and full-repository success is not claimed.

Update the judging field with the final three test files, PostgreSQL prerequisites/start/environment/stop commands and 34-pass result. Remove the obsolete optional reviewer note that described the two replay tests as still failing; optional notes may be blank.

Material boundaries remain: PostgreSQL process tests require POSIX process control and configured disposable PostgreSQL, and otherwise skip; failed launches require a deliberate new submission identity to retry; stale settlement occurs when polled or when new admission checks for conflicts, not via a startup sweep; unique-violation translation matches the named index in the wrapped database error text. These do not add unfulfilled requirements to this brief.

## Whole-task changed files (9)

- `api/server.py`
- `storage/database.py`
- `README.md`
- `scripts/scratch_postgres.sh` (new)
- `tests/api/test_enhanced_momentum_replay_workers.py`
- `tests/api/test_enhanced_momentum_backtest_job_durability.py` (new)
- `tests/api/test_enhanced_momentum_backtest_job_postgres.py` (new)
- `tests/api/_replay_process_harness.py` (new)
- `tests/api/BASELINE_FAILURES_enhanced_momentum_replay.md` (new historical notes)

Evidence: `.codex-attachments/task-a-check8-review/` contains acceptance output, scratch-cluster start/stop logs, verification runner, extracted-workspace metadata and final SHA-256 manifest.

This was a review, not synchronization into the active application checkout. Retain the final A export and original shared starter as the merge base while B is developed independently. Merge the actual A/B changes after both tasks complete rather than overwriting cumulative folders.
