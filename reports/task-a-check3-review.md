# Task A, Check 3 review

Reviewed cumulative export `55614414-1b70-451b-a93a-2b246322227c_3_a_changed_files` in a separate extraction of the shared starter. The active application source was not modified.

## Verified focused tests

The durability and existing replay test modules produced **14 passed, 2 failed**: all six durability tests pass; eight existing tests pass and the two known baseline failures remain. The screenshot's broader full-suite totals were not independently verified.

## Actual PostgreSQL verification

A disposable PostgreSQL 14.23 instance was created in a temporary directory, accessible only through its private Unix socket, and stopped and removed in a finally block. The production database was not used.

Using the export's actual Database wrapper and API functions:

- Applied the five relevant run/job migrations twice successfully, including the partial unique active-job index.
- Concurrent submissions from independent imported API module states with shared PostgreSQL connections returned one queued admission and one HTTP 409; exactly one job was persisted.
- Forced an actual unique-index violation through the Database wrapper; admission correctly translated that constraint into not-admitted rather than a storage 503.
- Settled a stale running job through the other API state; late status update was rejected and late result insertion raised ownership-lost. No result was inserted for that settled job.
- A valid new owner could save a result and persist completed status.

These are real database statements and concurrent connections with independent API module state, not separate operating-system API processes or a true process-kill test. They establish the tested SQL behaviors without pretending to validate the full application migration set or every crash race.

## Remaining scope

Submission retry identity is still missing: the POST request has no stable client submission key and generates a new job ID each time. Repeating identical parameters is not inherently a retry; a caller-supplied identity is needed to distinguish transport retries from intentional new runs. That should be the next implementation step, rather than a flag for this incremental response.

The result-save helper also permits two inserts when invoked twice while the same job remains running: an isolated PostgreSQL probe generated distinct run IDs. This is a demonstrated storage property, not proof that the current ordinary worker path calls it twice. Require safe repeated saves/recovery as part of idempotent submission work, without claiming a user-facing duplicate has already been reproduced on the normal path.

The AI corrected the ownership/admission issues and disclosed its lack of PostgreSQL testing. No new behavior flag is supported by this response. Automatic resumption of a partial calculation is outside scope; the absence of automatic retry does not itself violate the brief, but deliberate retry and lost-response submission identity do remain required.

Evidence: `.codex-attachments/task-a-check3-review/` contains the extracted workspace metadata, focused output, actual PostgreSQL probe, and results. The temporary PostgreSQL instance has been removed.
