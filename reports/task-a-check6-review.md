# Task A, Check 6 review

Reviewed cumulative export `55614414-1b70-451b-a93a-2b246322227c_6_a_changed_files` in an isolated shared-starter reconstruction. The live application source remains unchanged by this review.

## Independent results

- Durability + existing replay files: **26 passed, 2 failed**. All 18 durability cases pass, eight existing cases pass, and the known missing-helper/UTC-versus-IST expectations remain the only failures in that selection.
- Exported PostgreSQL file with an actual disposable UTF-8 PostgreSQL 14.23 server configured: **2 passed**, no skips. Scratch databases/server were removed afterward. The response's PostgreSQL 16 and full-suite counts are its reported results; only the selection above was independently verified here.
- Repeated the exact prior transport-error probe: one result remains saved, status and identity retry both return completed with run ID 4242, and replay calculation ran once.
- Repeated the failed-thread-start probe: same-identity retry and later poll both return failed, with durable failed state. The previous immortal-queued behavior is gone.

## Assessment

The previous two recovery defects are fixed under the reproduced conditions. Finalizing an interrupted/launch-failed job as failed and requiring an explicit new submission identity to retry is compatible with the brief. Transparent continuation of partial replay calculations was not required. The disclosed temporary difference between process memory and durable state during a failed status write is an eventual-reconciliation limitation, not by itself proof of a new failure. PostgreSQL tests being environment-gated is acceptable only when the final verification actually enables and executes them.

No new behavior flag is supported: the response follows the correction, demonstrates targeted regressions and discloses remaining limitations. No task extension is indicated merely to increase turn count.

Remaining finalization: align the stale replay checks with current helper contracts and IST session boundaries without weakening behavior assertions; verify representative admission/retry/ownership behavior with genuinely separate worker processes sharing disposable PostgreSQL; record exact setup and focused final acceptance results with no hidden skips. The present race tests and independent SQL probes use controlled process-state models/concurrent connections rather than a true operating-system process test, so the final verification should accurately distinguish those.

Evidence: `.codex-attachments/task-a-check6-review/` includes isolated workspace metadata, selected/PG outputs, and the repeated failure-path probes and results.
