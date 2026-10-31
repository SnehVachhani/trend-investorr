# Independent tasks after the completed LIVE-entry work

Research date: 29 October 2026. Baseline: active project after synchronizing the completed fourth task. No proposed task below has been implemented during this research.

## Recommended parallel pair

**A: Recoverable Enhanced Momentum backtest jobs** and **B: Reliable WebSocket subscriptions and reconnects**.

Both address reproducible failures, have meaningful intermediate review points, and can finish independently. A owns a historical-analysis job's lifecycle and persisted identity. B owns a live socket's requested subscriptions and connection lifecycle. Neither requires the other's implementation, nor changes to trading strategies, indicator formulas, or Task 4's reservation rules.

Estimates count one user prompt and its coding-AI response as one turn. They include investigation, implementation, review, and focused verification. They are planning ranges, not quotas; combine work or stop earlier when the agreed checks pass.

| ID | Task | Current evidence | Bounded outcome | Estimated turns |
| --- | --- | --- | --- | --- |
| A | Keep Enhanced Momentum backtest jobs recoverable after API restart | Job status and duplicate-run admission live in an in-memory dictionary. An empty worker registry returns 404 for another worker's job and accepts another job. | Persist job/request identity, progress and terminal outcomes; coordinate worker ownership; distinguish interrupted work from completed work; reconnect the UI to a saved result without repeating the computation. | 10–15 |
| B | Preserve live-feed subscriptions through disconnects and failures | Offline probes lose a failed subscription, subscribe a token cancelled before connection, send initial subscription twice, and report running after retry exhaustion. | Reconcile requested versus sent subscriptions; retain failed changes; restore the latest requested set after reconnect; make stop/restart and old callbacks safe; report actual lifecycle state. | 9–14 |
| C | Use verified broker P&L for the LIVE daily-loss limit | `OrderManager.update_prices()` sends the paper summary to the shared risk manager in LIVE mode. A controlled probe confirms that paper losses become LIVE risk inputs. | Establish the current intraday broker P&L source and scope; use broker loss evidence for LIVE, separate PAPER/DUAL loss decisions, preserve exits, and block affected entries when loss state is unverifiable. | 10–16 |
| D | Make trading pages distinguish failed requests from empty accounts | Orders and signals swallow load errors. Portfolio uses one `Promise.all`, discards successful sections when any request fails, and renders zero/empty defaults. | Orders, portfolio, signals and dashboard display separate loading, empty, partial, failed and stale states; provide safe retry; prevent old requests replacing newer data; preserve stock modal and sorting conventions. | 7–11 |
| E | Recover missed post-market analysis after restart | After-close startup backfills candles, computes indicators and warms caches, but does not execute the complete `post_market()` workflow that persists next-day scanner signals. | Persist the session's post-market stages, recover missed or incomplete work, refresh analysis inputs before scanning, and retry failed stages without duplicating successful signal output. | 9–14 |
| F | Enforce the configured per-sector position limit | `_check_position_limits()` logs a deferred sector check instead of rejecting. There is no sector-aware exposure count. | Resolve sectors consistently for held and pending positions, enforce the configured cap inside existing admission serialization, specify unknown-sector behavior, and preserve mode independence and exits. | 6–10 |

C through F are additional choices for later work. Their proposed acceptance conditions still need the same focused brief and baseline preparation as A and B.

## A. Recoverable Enhanced Momentum backtest jobs

### Evidence

- `api/server.py:135`: `_enhanced_momentum_backtest_jobs` is a process-local dictionary.
- `api/server.py:5066`: progress updates mutate that dictionary; no durable job record is written.
- `api/server.py:5152`: only this process's dictionary decides whether another replay is already queued/running.
- `api/server.py:5186`: a daemon thread executes the job; a process exit ends that worker.
- `api/server.py:5197`: status lookup returns 404 if the process does not know the job.
- `api/server.py:5216` and `storage/database.py:1128`: completed runs already have separate durable storage. This does not preserve an in-flight job's identity or link a saved run to a lost job acknowledgment.
- `frontend/src/components/EnhancedMomentumBacktestPanel.tsx:364`: the browser polls the persisted job ID; a 404 clears it and instructs the user to start again.

The research harness starts a job with execution deliberately paused. A second empty registry, simulating another worker process, cannot find it and accepts a second request. Clearing the registry, simulating process restart, loses the first job. These are controlled registry probes, not an actual multi-process database integration test.

### Clear goals

1. Persist the job ID, validated request, progress, state, timestamps, ownership information and result link using the project's storage conventions.
2. Admit/claim work atomically across API processes using shared storage. Preserve the existing policy on how many Enhanced Momentum replays may run; specify idempotent retry of a submission so a lost HTTP reply cannot silently duplicate work.
3. On restart, identify abandoned work honestly as interrupted or safely reclaim it under a defined ownership policy. Transparent continuation of a half-computed replay is not required.
4. Couple saved results with job completion so a crash after a result commit does not cause a second saved result or a false failure. Test the relevant crash boundaries.
5. Let the existing UI recover an in-progress job or open its completed result. Show an actionable interrupted state and explicit retry rather than suggesting a completed run disappeared.
6. Preserve existing run-history access and calculation outputs. Store failures must not report a successful durable admission.

### Likely ownership and checks

Primary files: `api/server.py`, `storage/database.py`, a dedicated job-store module, and `frontend/src/components/EnhancedMomentumBacktestPanel.tsx`. The job response type/client in `frontend/src/lib/api.ts` may need a small additive change. Existing engine math remains outside scope.

Checks: job and request survive a fresh service instance; polling works through another worker; two worker claims cannot execute the same job; abandoned workers cannot publish stale completion; interrupted jobs do not remain running forever; result-save/acknowledgment crash does not create duplicates; storage failure is visible; retry after a temporary polling failure restores the correct result. Use temporary storage and representative database transaction/claim tests, without real market services.

The two current test-contract failures described below should be inspected at baseline preparation. Do not silently relabel them as regressions or use them to justify changing strategy calculations.

## B. Reliable live-feed subscriptions and reconnects

### Evidence

`data/streams/websocket_manager.py` currently mixes desired subscription changes with its record of sent requests:

- Lines 166–181: unsubscribe considers only active tokens. It does not remove tokens queued before first connection.
- Lines 336–354: opening a connection first subscribes pending tokens, then subscribes the newly updated active set again.
- Lines 392–413: a transport exception is logged and swallowed; `_on_open` still clears pending tokens.
- Lines 415–436: failed unsubscribe is swallowed while its caller removes the token from local active state, so local state can claim success without a sent cancellation.
- Lines 235–274: reconnect exhaustion breaks the loop without clearing the manager's running flag. Retry sleeps also make shutdown coordination worth testing.
- Lines 184–234: start/stop use shared mutable state and one thread handle; a stale thread/callback must not control a newly started generation.
- Installed SDK compatibility checked during brief preparation: `SmartApi/smartWebSocketV2.py` forwards its close callback as `on_close(wsapp)`, while this manager requires three positional arguments. The SDK also implements its own retry/resubscribe behavior. B must test the actual callback contract and coordinate reconnect ownership, rather than relying only on simplified fake callbacks. No network connection was made for this inspection.

Independent fake-socket probes observed:

| Scenario | Current observation |
| --- | --- |
| Subscribe then unsubscribe before connection | The cancelled token is still sent, twice. |
| Subscription call fails on open; the next open succeeds | Pending state was cleared; the token is never retried. |
| Ordinary first subscription | Two identical subscribe calls. |
| Unsubscribe transport failure | Local active set loses the token and no pending cancellation remains. |
| Retry budget exhausted | `is_running` remains true after `_run()` exits. |

### Clear goals

1. Keep the latest requested token set as the authoritative intent, with connection-specific state for changes successfully sent. Distinguish sending a request from a broker acknowledgment if the actual protocol has no per-token acknowledgment.
2. Apply changes made before connection or during disconnection, including subscribe/unsubscribe reversals and repeated calls.
3. Retry failed subscription changes without forgetting them; restore only currently requested tokens when reconnecting.
4. Give reconnect/backoff a single clear owner compatible with the installed SDK. Shutdown must interrupt waits; restarting must not leave two active loops. Ignore callbacks from superseded sockets.
5. Report stopped, retrying, connected and exhausted states accurately through the manager's existing status interface.
6. Preserve the current Tick payload, callback contract, and the completed candle aggregation behavior. Bound capacity against the implementation actually supported; expanding to a general multi-connection feed pool is outside this task.

Primary file: `data/streams/websocket_manager.py`; add dedicated lifecycle/subscription tests. Keep any caller wiring minimal. Authentication redesign, API job storage, strategy logic, candle formulas, and order reservation changes are outside scope.

Tests should use fake sockets and controlled synchronization to force changes during disconnect/reconnect, transport failures, exhausted retries, stop during backoff, restart, and old callbacks. Preserve the existing candle-parser and startup tests. These are directly relevant regressions because this task changes their live-feed producer, rather than unrelated suites to pad the result.

## Additional task boundaries

### C. Broker P&L and LIVE daily-loss checks

`trading/order_manager.py:3018` updates paper prices and uses `get_daily_summary()` to overwrite the risk manager's daily realized/unrealized values without checking trading mode. The research probe sets LIVE mode and supplies a synthetic paper loss; those exact paper values are sent to the risk manager. `trading/risk_manager.py:301` uses those values for the daily-loss gate. Production callers of `CircuitBreaker.record_trade_result()` are also absent from the inspected main/trading/strategy path; that is a related discovery, not a requirement to build a full ledger in this task.

Bound the first task to current intraday daily-loss admission. Define broker P&L semantics from the installed adapter, include closed positions where required, reconcile repeated observations idempotently, and preserve conservative unknown-state behavior. Do not expand into fee accounting, a full execution ledger, historical portfolio reconstruction, or every circuit-breaker policy. Likely files: order/risk managers, a broker P&L adapter, main wiring, and focused loss-limit tests. Shares risk and entry-accounting boundaries with F; avoid running C and F together initially.

### D. Trading-page request failures

`frontend/src/app/orders/page.tsx:17`, `frontend/src/app/portfolio/page.tsx:25`, and `frontend/src/app/signals/page.tsx:22` swallow errors. Portfolio's zero defaults can present an unavailable service as an empty account. Dashboard has an initial error message but uses a single all-or-nothing load and silently retains stale status on polling errors.

Use independent section states, clear last-updated/stale treatment, safe read retries, and cancellation or request sequencing. Preserve successful sections when another endpoint fails. Tests should force success, genuine empty data, partial failure, complete outage, retry success, out-of-order replies and navigation/unmount. Keep this limited to read-only trading pages, not a global rewrite of every scanner/backtest panel. Preserve canonical stock details and sortable tables. No backend contract changes are necessary. The frontend currently has no test script; setting up a focused UI test harness is part of the estimate.

### E. Missed post-market processing

`main.py:1366` includes delivery data ingestion and delivery/pattern/MTF scanner results persisted for the next trading day. Startup's after-close path at `main.py:1610` only performs backfill, indicators and warm-up. It does not run those scanners and persist their output. Scheduled cron jobs look forward rather than automatically recovering a missed slot.

A useful bounded task is durable post-market completion per market session, with required stages and retryable failures. Refresh caches after ingestion before scanning; never mark the whole session complete after a swallowed stage failure. Reuse existing signal upsert identity (`storage/models.py:1636`) carefully: it currently resets status to ACTIVE on conflict, so a late repeat must not resurrect a consumed signal unintentionally. Include unavailable delivery publication and next-trading-day boundaries. Exclude a general exchange-calendar service and all broker exit changes. This task is larger than merely calling `post_market()` on startup.

### F. Sector concentration limits

`trading/risk_manager.py:292` counts all positions as a placeholder and only logs that the sector check is deferred. Proposed scope: consistently attach sector metadata to broker/paper exposure and pending reservations; use distinct instrument exposure, not separate fill rows, for the configured cap; enforce it at the serialized placement boundary; define missing classifications; preserve reducing exits and DUAL independence. Do not combine it with a broad new sector analytics/dashboard feature. Smaller than A–C, but still a real unfinished risk control.

## Independence and integration

| Pair | Assessment |
| --- | --- |
| A + B | Recommended. Separate job-store/API/UI lifecycle versus socket lifecycle. Existing candle and Tick contracts stay fixed. No required common implementation files. |
| A + C | Also independent. Historical jobs versus live loss-limit evidence. Some storage utilities may be shared without changing their contracts. |
| A + D | Independent if D stays on read-only trading pages. The shared API-client file may need a small textual merge; keep job polling owned by A. |
| B + D | Also a clean pair. Socket lifecycle versus read-only page request states. |
| B + E | Functionally separate but both may touch startup/main wiring; less convenient than A + B. |
| C + F | Avoid initially: both alter risk admission, mode separation and exposure metadata. Neither should build on an unstable version of the other's decisions. |

Prepare both starters from the same verified post-Task-4 source. Each coding session should have its own workspace. Each task must pass its own focused checks without the other task's implementation. When combining final exports, merge actual deltas against that common starting point; copying two cumulative folders over one another can revert shared-file changes. Then run the union of relevant checks once. Starter archives have not been created for these candidates yet.

## Verification performed during this research

1. Applied the final fourth-task patch to the active project, after backup and clean patch applicability checks. All 12 files match the reviewed final export.
2. Ran the agreed fourth-task acceptance selection on the active checkout: **236 passed in 11.27s**. See `reports/code-behaviors-task4-sync-20261029.md`.
3. Ran the offline candidate probes above, with fake sockets, stopped job threads, separate in-memory registries and synthetic P&L. No live account, real order, database mutation, network feed or expensive replay was used.
4. Ran:

```sh
.venv/bin/python -m pytest -q tests/api/test_enhanced_momentum_replay_workers.py tests/data/test_live_candle_pipeline.py tests/test_mid_session_restart.py
```

Observed baseline: **72 passed, 2 failed** out of 74. Candle pipeline: 27 passed. Session startup/restart: 37 passed. Enhanced replay workers: 8 passed, 2 failed.

Exact failing identities and causes:

- `test_replay_refreshes_ranked_cohorts_and_runs_setup_filters_on_active_members`: attempts to patch `_enhanced_backtest_profile_stock_batch`, which does not exist in the current API module.
- `test_backtest_loader_reads_only_warmup_and_requested_window`: expected UTC date boundaries, while the implementation sends Asia/Kolkata session boundaries.

These are baseline findings in the candidate area, not evidence that Task 4's 236-test acceptance selection failed. Determine the intended contract before repairing either test; equal counts alone are not proof of preexisting failures. A full-project green result is not claimed.

Evidence files: `.codex-attachments/parallel-task-research-20261029/probe_candidates.py`, `probe-results.json`, and `candidate-baseline-tests.log`.

The existing Graphify graph was used for orientation, followed by fresh source inspection because earlier graph data cannot establish current behavior. Turn estimates are judgment based on these concrete state transitions and test obligations; no turns should be added merely to reach the range.
