# Recommended fourth Code Behaviors task

Research date: 28 October 2026. Starting point: the local Trend-Investorr project after integration of all three completed task exports.

## Recommendation

**Make LIVE entry limits use verified broker exposure and durable pending-order reservations.**

A new LIVE BUY should be admitted only when verified broker positions, working orders, and unresolved local submissions show that it fits the configured position and deployment limits. Record its reservation before sending it, preserve uncertainty across restarts, and reconcile against broker evidence before releasing capacity. A submission response is an acknowledgement, not a fill.

This continues the earlier “accurate LIVE trading positions and fills” candidate with a narrower finish line. Task three implemented broker-based intraday exits; ordinary entries still have a separate, incomplete exposure path.

Planning estimate: high difficulty, roughly **12–20 substantive model exchanges**, with about 15 plausible. The code touches several interacting state transitions, rather than requiring 15 unrelated features. The actual turn count depends on the implementation and findings. Review stages below are checkpoints, not messages to send regardless of progress. Give the complete goal and acceptance conditions at the start.

## Context recovered from the prior chat

I read the saved conversation titled “Join Code Behaviors project.” On 27 October, the shortlist was mid-session restart recovery, accurate LIVE positions and fills, trading-page API error states, and persistent enhanced-backtest job status. The broad LIVE-state work was estimated at 15–25+ exchanges and was explicitly considered too large to take on without narrowing it.

On 28 October, a missed post-market analysis catch-up was considered, then replaced with the larger broker square-off task when a roughly 15-turn task was requested. The three completed tasks are now candle recovery, session restart handling, and broker square-off. The recommendation here accounts for their current integrated code, not the old starter.

Graphify supplied the earlier project context, but its graph does not describe the newly integrated order implementation completely. Findings below come from direct source inspection, the installed SDK source, and fresh offline probes.

## Verified evidence

All paths below are relative to the project root.

| Finding | Current source | Observed consequence |
| --- | --- | --- |
| The ordinary LIVE position getter returns paper positions. | `trading/order_manager.py:805` | With the broker already holding the target stock and `max_open_positions=1`, another BUY is submitted. The probe records zero broker position queries. |
| Broker verification is special-cased for SELL, not BUY. | `trading/order_manager.py:323` | A BUY is submitted even when the fake broker position service is configured to fail; it is never called. |
| Pending BUYs have no local durable reservation. | `trading/order_manager.py:636` | Repeating a BUY before it appears in broker books submits twice. Constructing a fresh manager with the same storage path also submits again. |
| Deployment checks sum only the supplied positions. | `trading/risk_manager.py:325` | With configured capital ₹100,000 and a 60% deployment cap, two accepted but unfilled BUYs each reserve no capacity and submit ₹100,000 combined estimated notional against a ₹60,000 cap. |
| The placement parser expects a dictionary, while the SDK method used returns an order ID string. | `trading/order_manager.py:670`, `trading/order_manager.py:776`; installed `SmartApi/smartConnect.py:326` | The fake HTTP layer accepts the order, but the app reports REJECTED with “str object has no attribute get.” |
| Any unsuccessful placement result releases an exit intent, including one caused by a lost reply or a parser exception. | `trading/order_manager.py:747` | Two repeated square-off calls send two accepted synthetic exits when the first is not yet visible in the order book, both with the actual SDK return shape and with a simulated timeout after acceptance. |

These are **eight separate diagnostic scenarios**, not eight failing pytest tests. They ran with controlled time and fake broker methods; no broker login or real order placement was involved. The SDK method was extracted from its installed source and supplied a fake HTTP response without importing its runtime networking setup.

The SDK finding is also supported by Angel One's official source: [`placeOrder` returns the ID; `placeOrderFullResponse` returns the response envelope](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py). The current session returns a real SmartConnect object directly (`data/auth/smartapi_auth.py:269`), so an existing application adapter is not correcting the mismatch. Current square-off test doubles instead return a dictionary (`tests/trading/test_live_squareoff.py:175`).

This means the three exports are synchronized, but their passing tests do not establish production compatibility with the SDK. The submission-outcome problem is a prerequisite within this task, including regression coverage for the shared exit path.

## Scope and acceptance conditions

1. **Broker exposure is authoritative for LIVE admission.** Read and validate the relevant position and order books. Identify instruments by exchange, token, and product, not symbol alone. Existing broker positions and working entries must affect the configured total-position, per-stock, and capital-deployment limits. Use the configured strategy capital budget; this task does not implement broker margin calculations. Establish explicitly which products belong to that budget. Do not treat delivery holdings as an intraday holding or modify them.
2. **Reserve pending entries before submission.** Reserve the proposed quantity, estimated notional, and position capacity durably before the broker call. Do not allow the same instrument or another instrument to consume capacity already reserved by unresolved submissions. Use a stable submission identity scoped to the account and trading day. No credentials belong in that identity.
3. **Distinguish acknowledgement, rejection, and uncertainty.** Handle the actual chosen SDK method's return contract. A valid order ID means acknowledged placement; a confirmed broker rejection permits release. A timeout, transport exception, malformed reply, or missing ID after an attempted send does not prove rejection and must retain the reservation and report uncertainty.
4. **Reconcile without double-counting or guessing.** Handle open, partial, filled, cancelled, and rejected orders. Count verified filled exposure plus the remaining working quantity once. Apply repeated observations idempotently. Do not release an unresolved reservation simply because a row is absent, an arbitrary timeout elapsed, or a single position snapshot is flat. If books disagree or cannot be interpreted, keep the conservative reservation and block affected new entries with a clear reason.
5. **Recover and serialize admission.** Reconcile at startup and before further entries. A crash after broker acceptance but before saving the acknowledgement must not allow a duplicate. Simultaneous calls must recheck all applicable limits and the entry hold when reserving capacity. Two local process instances sharing the configured store must not independently consume the same last slot or capital allocation. Multi-host distributed coordination is outside this task.
6. **Keep the mode-specific decisions separate.** PAPER continues using its own simulation without broker dependencies. In DUAL, paper approval and fills do not prove broker capacity or LIVE execution; report each leg's decision and result separately. Avoid combining paper holdings with broker holdings for a single LIVE limit calculation.
7. **Preserve the completed lifecycle fixes.** Coordinate with the square-off entry hold so a BUY approved before 15:15 cannot be sent after entries close. Reconcile pending entry reservations with square-off cancellations rather than forgetting them when positions appear flat. Preserve the restart boundary at 15:30, delivery exclusions, and the distinction between placed orders and fills. Apply the shared submission-outcome correction to existing exit placement paths too.
8. **Make the outcome reviewable.** Report why an entry was blocked, which state is unresolved, and what broker evidence resolved it. Persist identifiers and state sufficient to recover and test the decision. Add fake-broker regression tests demonstrating the current defects first, then passing after implementation. Acknowledged, partial, and completed quantities must not be conflated in audit output.

## Deliberate boundaries

The finish line is reliable admission and reservation accounting for the application's current long-only intraday entry path. It does not require a complete trading ledger, fee or tax calculations, new short-entry strategies, a broker P&L reconstruction, sector-cap implementation, new dashboards, changes to backtest strategy math, or a general rewrite of the scheduler.

In particular, `OrderManager.update_prices()` currently feeds risk P&L from the paper summary (`trading/order_manager.py:1725`), and closed-trade circuit-breaker updates are not wired through the ordinary production path. Those merit a separate broker P&L and loss-limit task. Completing this recommendation would improve the named position and deployment limits; it would not establish that all LIVE risk controls are correct.

Use the current three-task source as the starter. Do not pre-implement the fix before beginning the intended coding session. Likely affected areas are the order manager, risk manager, order-result models, a broker adapter or normalization layer, a durable reservation store, startup/reconciliation wiring in main.py, and focused tests. Exact file count depends on the chosen design.

## Fifteen natural review checkpoints

These are a work breakdown, not a promise of exactly fifteen prompts. Several may fit into one good response; actual defects may require more than one exchange. All requirements above should be visible from the first request.

| Checkpoint | Concrete work to inspect |
| --- | --- |
| 1 | Establish the current baseline and trace signal → risk decision → live placement. |
| 2 | Write failing cases for an existing broker holding, a missing position service, and pending BUY capacity. |
| 3 | Reproduce the installed SDK return shape and an accepted request whose response is lost. |
| 4 | Agree on the exposure budget, instrument identity, durable states, and reservation-release rules. |
| 5 | Implement and verify broker response normalization using an SDK-compatible test double. |
| 6 | Build the validated broker position/order snapshot and reject unreadable state conservatively. |
| 7 | Make configured position and deployment limits use broker exposure. |
| 8 | Add durable reservations before sending new BUY orders, including store-write failures. |
| 9 | Reconcile pending and partially filled entries without losing capacity or counting fills twice. |
| 10 | Handle terminal rejection/cancellation and prove exactly when capacity becomes available. |
| 11 | Test restart and crash points around reservation, submission, and acknowledgement. |
| 12 | Force same-process and shared-store contention for the final slot and remaining capital. |
| 13 | Verify PAPER independence and DUAL disagreement between paper and broker state. |
| 14 | Test overlap with square-off, cancellation races, missing replies on exits, and market-close boundaries. |
| 15 | Run the exact final regression command, inspect remaining limitations, and document the final files and outcomes. |

## Existing baseline actually run

From the project root:

```sh
.venv/bin/python -m pytest -q tests/trading tests/test_mid_session_restart.py tests/data/test_live_candle_pipeline.py tests/integration/test_signal_to_trade.py
```

Observed result on the synchronized source: **223 passed in 10.29s**. This is the selected trading and related regression baseline, not a full-project test run.

The task should add a dedicated suite for entry reservations/reconciliation and SDK outcome handling. Its future pass count cannot be specified until those tests exist. Keep the command above as a regression check and add the actual new test paths to the final reviewer command.

Read-only diagnostic:

```sh
.venv/bin/python .codex-attachments/task4-research-20261028/probe_live_entries.py
```

Its results are saved to `.codex-attachments/task4-research-20261028/offline-probe-results.json`. This diagnostic is research evidence rather than the task's final test suite.

## Other candidates considered

| Candidate | Current evidence | Assessment |
| --- | --- | --- |
| Persistent enhanced-backtest jobs | Jobs and their status updates live in a process-local dictionary (`api/server.py:135`, `:5066`); a missing job returns 404 (`:5196`) even though saved runs have separate endpoints. | Strong independent alternative. Durable job identity, interrupted-state handling, and linking committed results to jobs offer substantial work. It is the next choice if trading work is not the desired priority. |
| Missed post-market analysis | Startup catches up data and warm-up after close (`main.py:1561`), but does not invoke the complete `post_market` job registered with the scheduler. | Real gap, already discussed previously; likely smaller than the requested session unless expanded into durable per-day workflow recovery. |
| Trading-page API error states | Orders and portfolio swallow load exceptions (`frontend/src/app/orders/page.tsx:17`, `frontend/src/app/portfolio/page.tsx:25`). | Useful, but probably shorter than the requested roughly 15-turn task. |
| Sector position cap | `trading/risk_manager.py:287` still logs a deferred check instead of enforcing the cap. | Still unfinished, but the earlier chat correctly identified the narrow cap as too small on its own. |

Research created only these notes and diagnostic attachments. It did not change the production implementation or existing regression tests.
