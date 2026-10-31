# Task A, Check 7 review

Reviewed cumulative export `55614414-1b70-451b-a93a-2b246322227c_7_a_changed_files` against Check 6. Only the process harness, PostgreSQL test module and baseline note changed since that step. Runtime API/storage source is unchanged.

## Independent verification

Reconstructed the export over the shared starter, started a disposable UTF-8 PostgreSQL 14.23 server on a private Unix socket, and ran the exported PostgreSQL test module with its environment variables configured. Result: **6 passed in 12.95s**, no skipped tests. The scratch databases and server were removed afterward.

The harness genuinely spawns fresh interpreter processes. Each imports the API afresh, has its own registry/lock/worker identity, and opens its own database connections. Scanner evaluation is stubbed, while admission, the asynchronous worker lifecycle, heartbeats, settlement and result persistence use real runtime code. Tests assert one admitted different submission, same-identity convergence, and two deterministic stale-worker publication paths after SIGSTOP/SIGCONT. The stale worker cannot save a result, keeps no owned registry entry after forfeiture, and leaves the replacement row unchanged.

The six-pass repeated-loop claim in the screenshot was not independently repeated; this review ran one successful six-test selection. Existing durability/replay code is unchanged, so those tests were not rerun here; their last independently verified Check 6 result remains 26 passed and two known baseline failures. Full-suite screenshot totals are not independently verified.

## Finalization note

The different-submission admission test synchronizes POST entry with a barrier but does not explicitly keep the winner active until both submission decisions have completed. On a heavily delayed losing process, the winner could legally finish before the second admission, allowing a second job without violating the at-most-one-active policy. The current run passed, but make this test enforce overlapping admission windows using its existing stall events before the final acceptance pass. This is a test determinism improvement, not evidence of a new production defect.

No new behavior flag is supported. The response followed the request for genuinely separate processes and accurately disclosed POSIX/environment requirements. Larger participant counts, Windows portability of POSIX process-control tests, and broader performance stress are not required by this task.

Next step is final regression alignment and handoff: preserve semantic assertions while updating stale helper/time-boundary expectations, run the focused selection with PostgreSQL/process checks enabled and no hidden skips, give exact scratch-server setup plus commands/results, whole-task changed-file list and material limitations.

Evidence: `.codex-attachments/task-a-check7-review/` contains isolated workspace metadata, the disposable PostgreSQL runner and independent output. Production checkout code was not changed.
