# Two focused follow-up tasks grounded in the accepted work

Research date: 30 October 2026. Six completed implementations are present in the local source. The user reports the first three submissions were accepted; this research does not independently establish the platform's reason for accepting them or rejecting a later submission.

These are technical planning notes, not finished Taiga/Labelbox fields. The participant must author those fields and personal evaluations themselves.

## What the earlier tasks actually specified

Read the saved chat **Join Code Behaviors project**, including the candle, restart and broker square-off briefs and their final checks. Inspected the three original starter archives rather than assuming the most recent ZIP had identical packaging.

| Earlier task | Trigger and observable defect | Specific success criterion |
| --- | --- | --- |
| Candle pipeline | Cumulative traded volume was used as candle volume/VWAP weight; pending writes could be lost | The example cumulative-volume sequence gave volume 50 and VWAP 102 for the named interval; failed writes survived retry/restart |
| Mid-session restart | Starting at 11:00 missed the 08:30, 09:00 and 09:15 jobs | Before startup reached its idle loop, setup, warm-up and live components had run once in order; late starts did not reopen entries |
| Broker square-off | At 15:15 the broker held intraday exposure while the paper book was empty | Correct opposite-side broker exits; delivery untouched; accepted placement distinct from a confirmed close; repeated calls did not blindly duplicate exits |

The useful common structure is one existing operational failure, an identifiable entry point, concrete outcomes, and offline checks of the actual application path. Test counts, prose length and a target number of prompts do not explain platform acceptance by themselves. The later broad historical-repair/portfolio-engine proposals are not the recommended pair here.

## Recommendation 1: make the LIVE daily-loss stop use broker evidence

### Existing defect and fresh reproduction

`trading/order_manager.py:3018` always updates the risk manager from `PaperTrader.get_daily_summary()`, including LIVE and DUAL. `RiskManager.evaluate()` calls a shared daily-loss gate for both entries and reducing SELLs. Position-aware entry admission was corrected by an earlier task; the loss-evidence path was not.

Fresh controlled probes exercised the real OrderManager with the existing fake broker, a frozen market-hours clock and temporary reservation/audit storage:

| Scenario | Observed current behavior |
| --- | --- |
| ₹1 crore budget, 2% daily-loss limit; broker fixture shows a closed intraday trade buying for ₹5 lakh and selling for ₹2.5 lakh; paper P&L is zero | A new LIVE BUY was **PLACED**, despite the synthetic ₹2.5 lakh closed loss exceeding the ₹2 lakh limit |
| Paper realized loss is ₹2.5 lakh; broker fixture has no loss | The LIVE BUY was **REJECTED** using the paper loss |
| DUAL with that paper loss and a flat broker fixture | Both legs were rejected for the paper loss instead of deciding their loss gates independently |
| Broker still holds the target stock; a SELL would reduce it; paper loss exceeds the limit | The ordinary reducing SELL was **REJECTED** by the loss gate |

These probes establish wrong-source admission and exit behavior. They do not validate live broker P&L semantics. Angel One's official position-response example documents transaction quantities and amounts but does not guarantee a `realised`/`unrealised` pair. The implementation must establish its source contract rather than invent those fields. Reference: https://smartapi.angelone.in/docs (Get Position).

### Bounded goal

Make the existing intraday daily-loss limit protect the LIVE leg using verified current-day broker loss evidence, while keeping PAPER independent and genuine reducing exits available.

The work should resolve these connected requirements:

1. Define and implement a validated current-day INTRADAY P&L snapshot for the configured trading budget. Include closed/zero-net-quantity positions as well as open exposure; retain exchange/instrument/product identity. Use validated day executions/amounts and valid marks as required by the adapter contract. State whether P&L is gross or fee-adjusted; this task need not implement a new tax/fee ledger.
2. Check that evidence before admitting a LIVE entry, and recheck at the existing serialized placement boundary. Missing fields, contradictory rows, nonfinite amounts, stale marks, unreadable responses and wrong-day evidence must not become zero loss. Unknown evidence blocks the affected LIVE entry with a useful reason.
3. Keep PAPER and LIVE loss inputs and decisions separate in DUAL. Do not temporarily overwrite one shared risk manager's P&L between legs in a way concurrent calls can observe. Mode changes must not reuse paper evidence as broker evidence.
4. Define a daily stop: after verified LIVE loss reaches the configured limit, close new LIVE entries for the remainder of that trading day. Preserve that stop across a same-day restart/reset and shared-storage instances. A new day's entry eligibility requires new-day evidence. Use the existing hold/reservation conventions where suitable; do not reopen a square-off hold while resetting loss state.
5. A verified reducing SELL or scheduled square-off must remain possible after a loss stop or an unverifiable entry-loss snapshot. Preserve existing checks that prevent an oversized/unverified SELL from opening a short or duplicating an outstanding exit. A loss stop does not itself liquidate positions.
6. Expose which leg was blocked, the evidence day/freshness and the reason. Add real-path regressions for both wrong-source directions, closed losses, stale/unavailable evidence, the threshold boundary, same-day restart, day rollover, concurrent decisions and independent DUAL outcomes.

Keep the scope on this one daily-loss rule. No sector-cap implementation, new strategies, portfolio reporting overhaul, broker margin calculation, full execution ledger or redesign of the circuit breaker's other policies is required.

### Starting files and tests

Primary files: `trading/order_manager.py`, `trading/risk_manager.py`, a small broker-loss normalization module and day-scoped hold storage if needed. `main.py` may need minimal loss-refresh wiring. Existing position/order verification and reservation contracts should be preserved.

From the new archive's `AlgoAnalyst` folder after installing both manifests:

```sh
/tmp/venv/bin/python -m pytest -q tests/trading/test_risk_manager.py tests/trading/test_live_buy_broker_state.py tests/trading/test_live_sell_exposure.py tests/trading/test_paper_trader.py
```

Verified starter baseline: **79 passed**. These are existing protections, not tests that already cover the new defect. The final selection must include the new loss-evidence tests, whose paths and total counts are not known yet. Use fake broker responses and temporary storage; no real order or broker login is required. If production writes use a new database transaction, verify that transaction separately against an appropriate disposable store.

Planning estimate: **14–20 substantive exchanges**, including investigation and review, with 15 plausible. This is an estimate rather than a conversation quota.

## Recommendation 2: recover the missed post-market signal batch

### Existing defect and fresh reproduction

`main.py:1368` defines post-market analysis. It fetches/stores bhavcopy, catches up historical data, computes indicators, runs delivery/pattern/MTF scanners and publishes next-day signals. `scheduler/market_scheduler.py` schedules it at 16:00. The after-close startup branch only catches up historical data, indicators and scanner warm-up; it does not execute that post-market job.

A fresh probe booted the real `main()` at **16:30 IST on Wednesday 4 March 2026**, using the accepted restart suite's fake services and controlled clock. Before the application reached its idle loop:

- The next post-market job was scheduled for **5 March at 16:00**, not the current session.
- No post-market job had fired and bhavcopy had not been requested.
- Backfill, indicator and warm-up calls had occurred, while the live feed correctly remained stopped.

When the same real post-market closure was explicitly invoked with a synthetic bhavcopy failure, it swallowed the exception and the scheduler reported **success**. Thus simply calling the existing function on restart is insufficient: required input failures and publication must have honest completion semantics.

Additional source findings: the post-market scanner call uses the in-memory `_candle_cache`/`_indicator_cache`, so fresh database writes alone do not prove fresh scanner inputs. `ScannerSignalStore.insert_batch()` catches individual write failures and returns a count; repeated upserts set status ACTIVE. A retried run must not silently publish a partial batch, leave obsolete signals from an earlier result, or reactivate a signal already consumed for that target session.

### Bounded goal

Ensure the most recent eligible post-market session produces one complete, correctly dated next-day scanner-signal batch, even if the process starts late or a required step fails and is retried.

The connected requirements are:

1. Both the scheduled 16:00 job and startup recovery use the same session-aware workflow. Before the current day's cutoff, leave its scheduled job alone. After it, recover missed/incomplete work. An eligible unfinished prior session should also be recoverable before the next session's warm-up consumes its output. Use the existing calendar consistently for weekends/holidays and target-session calculation.
2. Carry the analysis session date explicitly through data requests, scanners, publication and recovery. An older bhavcopy returned by the current fallback behavior cannot prove the requested day complete. Treat unavailable required inputs as pending/failed and retry through a bounded policy, without running live services or placing orders after close.
3. Build scanner inputs from the data/indicators refreshed for that run. A stale morning cache or an empty cache caused by a load failure is not a successful current-day scan. Distinguish a verified scan with zero signals from a scan that never ran.
4. Record a durable per-session outcome/checkpoint sufficient to retry incomplete work and skip verified completed work across restarts. A failure must not be journaled as complete merely because the closure returned. A scheduled call overlapping startup/manual recovery cannot publish a competing batch for the same session.
5. Publish the session's delivery/pattern/MTF output consistently. A crash or write failure must not expose a half-written replacement as completed. Repeating a successful publication must converge on the same output. Handle removed/changed/zero-result signals without stale leftovers, and do not reactivate already consumed signals on a retry.
6. Completion is true only after required data preparation, scan and publication have succeeded. Report the analysis date, target session and failed/pending stage. Cleanup of old raw ticks is maintenance, not proof that the analysis succeeded. Keep prior morning startup, market-close and trading-entry safeguards unchanged.

Keep the scope on producing this existing next-day scanner batch. Do not replace the scheduler, build a generic workflow platform, redesign candle-gap discovery, add scanner formulas, recover every historical session, or redesign live trade execution. New workflow/service code can be small and specific to this job.

### Starting files and tests

Primary files: the post-market/startup portions of `main.py`, a focused post-market service/checkpoint module, `ScannerSignalStore` in `storage/models.py`, and additive persistence if necessary. `scheduler/market_scheduler.py` only needs changes when the existing job wrapper/registration cannot express the required scheduling behavior.

Existing baseline:

```sh
/tmp/venv/bin/python -m pytest -q tests/test_mid_session_restart.py
```

Verified starter baseline: **37 passed**. The new post-market suite must exercise real startup/workflow code with a fake clock, provider, scanners and durable temporary store. Include 15:59/16:00/16:30 starts, a same-session restart, next-morning recovery, a weekend/holiday boundary, unavailable/wrong-day input, current-data cache refresh, valid zero output, mid-publication failure, repeated publication and overlapping scheduled/recovery calls. Test startup and saved signal output, not only calls to private helpers.

The final new test paths, command and actual counts must be established by implementation. No broker credentials, market connection or production database is needed for the main selection. Transaction-level publication checks should use a disposable test database if the selected persistence design needs them.

Planning estimate: **12–18 substantive exchanges**. The missed-call fix alone is smaller; the estimate includes its necessary input-date, retry and consistent-publication behavior. Do not add unrelated features to reach 15 turns.

## Independence

The loss-stop task owns intraday order admission and loss evidence. Post-market recovery owns after-close scanner inputs and publication. They can each pass against the common starter without the other's implementation. The likely shared file is `main.py`, but the wiring occurs in different lifecycle paths. Merge actual deltas against the common starting ZIP rather than copying two cumulative folders over one another.

## Starter packaging compared with the first three tasks

Inspected these original archives:

- `AlgoAnalyst-Taiga-Staging.zip`: 159 files.
- `AlgoAnalyst-Restart-Staging.zip`: 159 files.
- `AlgoAnalyst-Live-Squareoff-Starter.zip`: 162 files.

All three used one Python-project root, `.gitignore`, runtime and development requirements, source and tests including the scheduler, and excluded Git/agent instruction files. They were not whole-machine/project-directory dumps.

The new `/Users/macbookpro/Downloads/AlgoAnalyst-Current.zip` follows that structure, with root **AlgoAnalyst** and 179 files. It includes current source for all six completed implementations. It excludes frontend/Java, `.git`, AGENTS/CLAUDE/skills/workflow notes, local `.env` and `.env.example`, vaults, credentials, runtime/cache data, logs, environments, prior exports and this research. The extra files versus the old ZIPs principally reflect completed source/tests; this is not a return to an older implementation.

The application source and requirements match the synchronized checkout. Packaging-only differences are a current technical README and stronger ignore rules. All packaged Python source parsed successfully. ZIP integrity was checked. The scan for common key/private-key/JWT/email/personal-path patterns found no findings; it is not an infallible guarantee. Synthetic security fixtures remain runnable.

From a fresh extraction, the exact five-file union of the two baseline commands produced **116 passed, 0 failures, 0 errors and 0 skips** using the existing isolated Python 3.12 environment. This was not a new dependency installation or a full-suite success claim. The existing unrelated failing suites are retained rather than removed.

Research evidence is in `.codex-attachments/focused-task-research-20261030/`: original-archive comparison, probes/results, source manifest, package metadata, extracted-workspace test log and JUnit counts. No proposed fix was implemented in the project or ZIP.

## Submission boundary

This research cannot certify platform acceptance, and the user's report that earlier work was accepted does not establish an exception to current eligibility rules. The previously checked per-repository/public-source restrictions remain unresolved for additional submissions. Submission descriptions and personal judgments must be written by the participant; these notes provide technical facts, not a replacement for that authorship.
