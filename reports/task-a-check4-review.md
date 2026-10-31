# Task A, Check 4 review

Cumulative export `55614414-1b70-451b-a93a-2b246322227c_4_a_changed_files` was reconstructed over the shared backend starter in an isolated directory. Active source was not changed.

## Independent selected result

The durability and replay test modules produced **21 passed, 2 failed**: 13 durability cases, eight existing successes and the same two known baseline failures. Broader screenshot counts were not independently verified. Several reported pre-fix cases are missing-field KeyErrors; the broader behavioral cases, rather than those alone, establish retry defects.

## Actual PostgreSQL checks

A disposable PostgreSQL 14.23 instance was initialized on a private Unix socket, tested, stopped and removed. Used the exported Database wrapper/API code and independent imported API module state with concurrent database connections, not separate OS API processes.

Verified:

- All six relevant run/job migrations apply twice to a fresh schema.
- One distinct submission is admitted while the competing submission receives 409; active-job constraint errors map to busy.
- Expired owner status and result writes remain rejected.
- Valid owner result saving and completion work.
- Repeating result saving for one running job returns the same saved run ID, with no extra row.
- Cross-worker identity lookup returns the original job; different parameters for the same identity produce 422.

## Remaining reproducible issues

### Same-identity retries can still get 409

The first identity lookup happens before the active-job checks. Pause two workers after both read no identity; allow A to admit; resume B with its earlier empty lookup. B then sees A through the active-job guard and immediately raises 409, never reaching the later identity reread. The actual PostgreSQL probe returned one job ID and HTTP 409 for the same identity.

The added concurrency test forces reads to remain stale through the admission operation, so it exercises the later fallback but misses this earlier guard interleaving. Check both the local-memory active guard and the other-worker active guard: each needs to resolve an already-admitted matching identity appropriately. Do not allow different submission IDs to bypass the capacity limit.

### Previously applied nonunique index is not upgraded

Applied the previous version's nonunique `idx_enhanced_momentum_backtests_job`, then reran the current migration. `CREATE UNIQUE INDEX IF NOT EXISTS` leaves that index nonunique; the result insert fails with “there is no unique or exclusion constraint matching the ON CONFLICT specification.” This issue is disclosed accurately in the response. It applies when an intermediate version was installed, not to the fresh starter schema. A corrective additive migration should avoid silently discarding saved results and explicitly handle duplicate existing job IDs.

No new behavior flag is supported: these are implementation gaps, and the response openly acknowledges database/migration uncertainty. Global single-operator identity scope and no TTL do not independently violate this brief. The response's tests do not establish all retry interleavings.

Evidence: `.codex-attachments/task-a-check4-review/` contains selected test output, actual PostgreSQL probe/results, and the isolated workspace metadata. Both database instances and code changes stayed separate from the production checkout/database.
