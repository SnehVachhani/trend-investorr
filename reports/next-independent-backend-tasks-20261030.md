# Two larger independent backend tasks

Research date: 30 October 2026. Starting point: the synchronized Task A and Task B implementations. These are technical planning notes, not text to paste into Taiga or Labelbox; the current project instructions require participants to write their submitted descriptions and feedback themselves.

## Recommended pair

| Candidate | Work | Type | Planning estimate |
| --- | --- | --- | --- |
| Candle repair | Detect interior historical gaps and repair them through a durable, resumable, concurrency-safe workflow | Existing correctness defect plus a larger recovery feature | Approximately 16–24 substantial prompt/response turns |
| Shared portfolio replay | Add an optional chronological, shared-cash portfolio simulation across the stocks selected for a fundamental-query technical backtest | New backend feature, preserving the existing independent-wallet mode | Approximately 18–26 substantial prompt/response turns |

The estimates assume investigation, implementation, adversarial review, correction and final verification. They are not minimum quotas or promises. No unnecessary work or idle time should be added if a model finishes sooner. Each scope should be finalized before starting and kept within the project's time expectation.

The review covered the current ingestion and coverage paths, historical fetching, live feed and candle boundaries, trading/risk state, replay job persistence, fundamental and filter backtesting, API/startup wiring, schema/store conventions and relevant tests. The existing graph was consulted for orientation; it did not establish these defects. The evidence below comes from current source and controlled probes.

## Candle repair: completeness and restart recovery

### Observed starting behavior

`data/data_ingestion.py:180` uses coverage boundaries to decide whether to skip backfill. `_is_interval_current()` and incremental updates at lines 556–567 inspect the latest timestamp. `storage/models.py:1242` estimates missing history from boundary span. The recent-session query at line 1303 finds sessions with an opening or closing bar absent, using groups that already contain rows. These mechanisms do not establish that interior bars or entire sessions are present.

An offline probe constructed a regular five-minute session with 74 of its 75 expected synthetic bars, retaining both endpoints and omitting 11:00 IST. With coverage spanning the requested period, backfill skipped the stock, made zero provider calls, and the latest-timestamp test said it was current. This is evidence of a planning defect; it does not mean every missing timestamp in real illiquid-stock history necessarily represents a provider error.

Coverage writes are separate from candle insertion. `CandleCoverageStore.upsert()` at line 1113 sets its count to the supplied batch count, rather than deriving total stored rows. A latest timestamp, an empty response, a successful HTTP request and verified historical completeness need distinct meanings.

### Proposed bounded outcome

1. An explicit repair request identifies exchange, stock identities, interval and date window. A dry run reports suspected missing ranges and the basis for the decision, including interior gaps and fully absent sessions. It must not infer complete history merely from first/last timestamps or a matching count.
2. Session planning handles the supported intraday intervals and daily data using an explicit exchange-session calendar, timezone normalization and last-completed-bar rules. Weekends, supplied holidays, shortened sessions, listing boundaries and current unfinished bars need defined treatment. Calendar inputs should be injectable in tests; downloading or maintaining every future exchange calendar is outside scope.
3. Distinguish present data, a suspected missing bar, a successful empty provider window, invalid data and failed/unverifiable requests. Do not manufacture candles or assume all illiquid instruments trade in every slot. Successful emptiness must be recorded for the exact checked window, with an explicit recheck policy, rather than globally suppressing repairs.
4. Persist repair plans and bounded chunks. Restart resumes pending/failed chunks without repeating all completed work. Support interruption/cancellation between chunks, bounded retries and the existing provider pacing. A dry run must perform no provider fetches or writes.
5. Two workers sharing the same storage cannot own and finalize the same chunk concurrently. Expired ownership must be fenced at publication. Enforce repair identity using exchange, stock, interval and range; token or symbol alone is insufficient. Different nonconflicting chunks may still progress.
6. Validate response identity, range, timezone, OHLCV, ordering and duplicates. A failed or partially invalid batch cannot advance a success checkpoint beyond what was actually accepted. Candle upserts, chunk outcome and relevant coverage changes must commit consistently; retries after a lost acknowledgment must converge on one stored result.
7. Refresh affected derived daily/weekly buckets and coverage after historical corrections. Preserve authoritative daily delivery fields and source precedence. Derived data must not be reported as complete from incomplete source groups. Unrelated intervals and stocks must remain untouched.
8. Wire the repair behavior into the existing historical startup/manual catch-up path, with progress that distinguishes downloaded, stored, verified, empty, unresolved and failed work. A partially unsuccessful repair must not make startup report its data as fully verified.

Keep live WebSocket lifecycle, tick aggregation semantics, order placement, strategy formulas and the frontend outside this task. Do not build a second generic job platform merely to reproduce the recently completed replay job work.

### Ownership and acceptance evidence

Primary ownership: `data/data_ingestion.py`, `data/fetchers/historical_fetcher.py`, candle/coverage portions of `storage/models.py`, a new repair planner/store module and additive schema if needed. `main.py` and the existing ingestion-related API helpers may need small wiring changes.

Existing relevant baseline command, from `AlgoAnalystBackend` after the common requirements setup:

```sh
/tmp/venv/bin/python -m pytest -q tests/data/test_historical_fetcher.py tests/data/test_incremental_sync.py tests/api/test_startup_candle_sync.py
```

In the extracted starter: **18 passed, 10 failed**. Seven failures are in incremental synchronization and three in startup sync. Some assertions describe older call signatures/default exchange behavior, and four timestamp cases currently compare naive and aware datetimes. Inspect each cause before changing tests. New repair tests and the verified final pass/failure/error/skip counts must be added at completion; no final count is predicted now.

New evidence should cover an interior hole with both endpoints present; an entirely absent session; duplicate rows masking a missing timestamp; empty/invalid/partial provider data; holidays and shortened sessions; cancellation/restart; storage failure before/after commit; same-chunk ownership races; stale workers; bounded retries; corrected candles and dependent aggregates; and honest partial startup status. Use synthetic market data and a fake provider. Keep the main selection database-free through a stateful temporary store/fake; transaction/migration behavior should also receive a targeted disposable-database check when the implementation changes SQL. Never use a production database for verification.

Expected review stages: reproduce the completeness defect; define session/window semantics; implement planning; define empty/error evidence; persist chunks; resume after failure; coordinate owners; fence commits; propagate corrections; integrate status; force race/crash cases; verify compatibility; final handoff. Several stages naturally require both an implementation turn and a correction/review turn.

## Shared portfolio replay: one cash balance across stocks

### Observed starting behavior

The current fundamental-query technical backtest explicitly documents independent wallets. `api/server.py:8981` fixes capital at ₹100,000 per stock; `_run_fundamental_setup_stock()` at line 9304 simulates one stock independently. Lines 9536–9556 add the separate results, and drawdown is a mean of individual stock drawdowns. Line 9594 openly records that the result does not simulate a shared cash balance.

An offline probe ran the existing sweep-reclaim fixture for two stocks. Both entered at the same timestamp, and their combined gross entry notional was approximately ₹199,612 because each simulation received ₹100,000. This is correct for the existing mode. A portfolio capped at ₹100,000 requires a different simulation, not a change to the labels or a post-processing adjustment to those trades.

`FundamentalQueryBacktestRequest` at line 9009 has no portfolio-cash allocation policy. The existing standalone and filter-backed setup engines expose substantial reusable signal/risk logic, but their execution currently couples decisions to separate per-stock cash balances.

### Proposed bounded outcome

1. Add an optional portfolio mode to the existing fundamental-query technical backtest API, with total initial capital, maximum concurrent holdings and a per-position allocation cap. Preserve the existing default mode and response behavior for old clients.
2. For each selected setup, simulate one independent shared-cash portfolio across the fixed selected stock universe. Different setups remain separate experiments; do not accidentally make them compete for the same wallet. Reuse all currently supported standalone and filter-backed setup definitions.
3. Generate point-in-time entry candidates and exits from the existing strategy rules, then execute them against a single chronological multi-stock event clock. Do not run independent full-capital backtests and later discard or resize finished trades: capital decisions affect subsequent quantities, re-entries and results.
4. Specify event ordering explicitly. Open-known exits can release cash before that opening's entries. An exit triggered later by a candle's high/low cannot finance another stock's entry at that same candle's open. Signals only become eligible after their evidence is available; daily context must be complete at the decision time.
5. Simultaneous candidates compete under a deterministic rule using information available then. Permuting input stocks or changing data-loading worker count must not change allocations. Enforce cash, position and allocation limits with whole shares and fees included; do not silently use leverage. Record rejected or expired candidates with understandable reasons.
6. Preserve existing stop, target, holding-period and slippage rules, including gap-through behavior and the declared conservative same-bar stop/target policy. Use the existing charge calculator. Distinguish known opening fills from intrabar assumptions and make those assumptions visible in the result.
7. Define missing/stale-price and terminal-position behavior. Never substitute another stock's bar, a future quote or zero for an unavailable price. Track valuation age and unresolved values honestly; do not claim a fully settled final portfolio when a held position cannot be priced or exited under the declared policy.
8. Build one cash/holdings/equity ledger and derive portfolio returns, fees and maximum drawdown from that event stream, rather than averaging per-stock percentages. Cash plus marked holdings must reconcile to equity, and trade cash flows must reconcile to the ledger.
9. Persist the portfolio settings, resolved universe, execution-policy version and sufficient input identifiers/fingerprints with the existing saved result. Repeated runs of the same supplied snapshot must be reproducible. This does not require storing all production history in the result or guaranteeing identical outputs after source data changes.
10. Return mode-specific results through the existing start/status/result/history flow. Validate impossible/nonfinite settings and surface partial stock failures without presenting them as successful zero-return investments. Preserve existing independent-wallet outputs, preset formulas and completed Enhanced Momentum job behavior.

Keep this backend-only, long-only and cash-funded. No live broker integration, margin/shorts, tax-law changes, new frontend, generic job-system rewrite, new fundamentals provider, or historical index-membership reconstruction is required. The selected universe is an explicit fixed input; this task must not claim to solve survivorship bias in upstream selection.

### Ownership and acceptance evidence

Primary ownership: a new portfolio replay engine under `strategies/`, reusable candidate-generation interfaces in `strategies/fundamental_setup_backtester.py` and `strategies/profile_backtester.py`, and the fundamental-backtest request/result orchestration in `api/server.py`. Existing JSON result storage may suffice; keep any persistence extension additive and limited to result metadata.

Existing relevant baseline command, from `AlgoAnalystBackend` after the common requirements setup:

```sh
/tmp/venv/bin/python -m pytest -q tests/strategies/test_fundamental_setup_backtester.py tests/strategies/test_profile_backtester.py tests/api/test_fundamental_query_api.py tests/trading/test_charges.py
```

In the extracted starter: **47 passed, 2 failed**. One setup-catalog assertion omits the current swing-volume-profile setup; one execution-window assertion expects pre-normalization UTC timestamps. Preserve their behavioral purpose while checking the current contracts. New portfolio-engine/API tests and actual final counts remain to be established.

New evidence should include simultaneous signals exceeding cash; slot contention; cash released at the open versus later intrabar; fee-aware sizing; gaps and dual stop/target touches; missing and asynchronous bars; end-of-window holdings; no-future-information checks; changing future candles without changing earlier decisions; permutation/worker-count invariance; ledger identities; empty/partial/failed datasets; settings/result persistence; and compatibility with every supported setup and the old mode. Synthetic candles and fake storage are sufficient for the main acceptance selection; no market connection or PostgreSQL server is required for that selection.

Expected review stages: characterize existing independent wallets; define portfolio/event contracts; separate signal generation; build merged clock; implement cash and sizing; enforce slots/ties; integrate standalone exits; integrate filter-backed setups; align timeframes; handle gaps/missing bars; produce ledger and metrics; persist/API-wire results; test no-lookahead and determinism; compare old-mode outputs; final verification. These have real implementation dependencies rather than arbitrary prompt splitting.

## Independence and later integration

The repair task owns how historical rows are discovered, fetched and repaired. Portfolio replay owns how supplied candle snapshots are simulated. Portfolio tests use fixed synthetic inputs and must pass on the original reader without the repair implementation. Repair tests must pass without a new portfolio engine.

Likely shared files are `api/server.py`, possibly `storage/database.py`, and documentation. These are textual merge surfaces, not functional dependencies. Keep repair changes out of fundamental portfolio execution; keep portfolio changes out of ingestion/store planning. Each session starts from its own extraction of the exact same ZIP. Combine final deltas against this common starter and run the union of relevant checks after both finish.

## Starter archive and verification

Archive: `/Users/macbookpro/Downloads/AlgoAnalystBackend-20261030.zip`.
Internal root: `AlgoAnalystBackend`.
Staged folder: `/Users/macbookpro/Documents/Codex/AlgoAnalystBackend-20261030/AlgoAnalystBackend`.

The archive contains 180 files: backend source, tests, manifests, schema code, static sector data, placeholder `.env.example`, `.gitignore`, a technical README and the existing disposable PostgreSQL helper. It includes the just-synchronized completed tasks. It excludes frontend/Java services, `.git`, AGENTS/CLAUDE files, skills/workflow notes, old exports, runtime/private data, real environment files, vaults, caches, logs and virtual environments.

Only README and `.gitignore` are packaging adaptations; application source and dependency manifests match the synchronized project. All packaged Python files passed syntax parsing. The pattern scan found no credential-shaped strings or email addresses; this is a scan result, not an infallible guarantee. Tests retain synthetic identifiers and generate any temporary security keys themselves.

Verification ran from a fresh extraction using the already installed isolated Python 3.12 environment, not a claimed new dependency install. The combined candidate baselines and the completed tasks' database-free checks produced **165 passed, 12 failed, no collection errors or skips**. Of these, the completed tasks' checks remain **100 passed**; the new task baselines are **65 passed, 12 failed**. The README records the latter starting state. The PostgreSQL suite was not included in this packaging run; its six checks passed during the immediately preceding synchronization.

The initial full-project baseline had one additional status-test failure because shared startup-store state differed in that environment. It passed from the isolated source extraction; do not claim the full project and clean bundle had identical failure identities/counts.

Research logs, probe code/results, archive/source hash manifests, scan results, guide observations and per-file test counts are in `.codex-attachments/next-parallel-research-20261030/`. The source project was not modified to implement either candidate.

## Current Code Behaviors constraints

The live Instructions document was successfully read through the Google Drive connector on 30 October 2026. FAQ still limits one repository to five submissions. Safe Practices still says public open-source repositories only. Ownership alone is not an explicit exception in that text. Payment guidelines and Safe Practices also require human-authored submission descriptions and feedback. These recommendations and archive do not establish eligibility for another paid submission, and renaming the ZIP does not create a new repository.

Before another submission, establish that the repository is eligible under those rules or obtain a project-team exception. This does not prevent preparing or using the technical work locally.
