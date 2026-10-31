# Task A, Check 5 review

Reviewed cumulative export `55614414-1b70-451b-a93a-2b246322227c_5_a_changed_files` in a separate shared-starter extraction. No production source or database was changed.

## Verified results

- Durability + existing replay selection: **23 passed, 2 failed** (15 durability successes, eight existing successes, and the original two baseline failures).
- Both exported PostgreSQL tests: **2 passed in 1.58s**, neither skipped, against a disposable PostgreSQL 14.23 server using UTF-8. This exercises the full migration routine, upgrade from the old nonunique index with duplicate result rows, idempotent/missing-index repair, real admission/fence/result-save/identity SQL. All scratch databases and the temporary server were removed. The initial locally created SQL_ASCII scratch cluster could not encode an existing migration comment; the harness was corrected to UTF-8 and rerun without modifying application source. The initial setup failure log is preserved separately.

Source inspection and the two added unit regressions confirm that both early busy guards now route through identity rereading. Existing stored outcomes and different-identity capacity rejection remain protected. The duplicate migration retains every result row, keeps the job's referenced result or earliest result linked, and unlinks extra duplicate results explicitly; it does not delete them. Broader full-suite screenshot counts and actual two-OS-process races were not independently verified in this review.

## Additional independently reproduced recovery defects

1. **Committed result misreported as failed after a second save error.** Use the real worker with stubbed replay and exported fake storage. The first result insert commits, then raises a lost-response error. The single save retry also raises a transport error, while subsequent reads/status writes work. The worker catches the exception and persists failed. One saved run exists, but retrying the original submission from another API state returns failed with no run ID. `_settle_enhanced_backtest_job` returns terminal jobs unchanged, so recovery cannot repair it. This contradicts the requirement to recover already-saved results rather than report them as a failed calculation. This is a meaningful compound transport case, not a demand for unbounded retries.
2. **Failed thread launch strands an owned queued job.** Inject an exception from the worker thread's start method after admission. The queued job remains in this process's registry. Same-identity retries return it directly without launching anything; polling still returns queued even after the stored heartbeat is made an hour old. No heartbeat worker exists to correct the state. Report launch failure durably and clear/reconcile the local cache, or implement a safe single-launch recovery policy.

These are technical recovery gaps within the original brief, not evidence of deception. No new behavior flag is supported: the response addressed the requested fixes, tested PostgreSQL itself, and disclosed remaining scope honestly. No separate frontend work, arbitrary retention policy or multi-tenant identity scope is required.

Next prompt should correct these two failure paths with reproduction-first regressions and retain the ownership/idempotency guarantees. After that, the remaining finalization includes resolving the two stale baseline test contracts and recording exact acceptance setup/commands/results, with actual PostgreSQL execution rather than hidden skips.

Evidence: `.codex-attachments/task-a-check5-review/` contains isolated workspace metadata, focused output, PG harness/output, failure-path probe and results.
