# Task B, Check 5 review

Reviewed cumulative export `f1dbbcd4-1689-4182-8641-125ec53b0e35_5_a_changed_files` in an isolated reconstruction of the shared starter. Manager now captures socket-generation IDs in callback closures, tracks run IDs/per-run stop events, and serializes concurrent start calls with its subscription RLock. Shared SDK fakes and a new generation test module were added. Active source was not changed.

## Independent focused result

The four focused files yielded **55 passed, 2 failed**, not the reported 57-pass result in this environment:

- `test_replacement_connection_receives_only_latest_requested_subscriptions`
- `test_stop_then_start_while_old_connect_is_still_finishing`

Both observed an empty fake frame list immediately after waiting for `is_connected`. `_on_open` sets that flag before `_sync_locked` sends frames, and the getter is unlocked, so these tests race callback completion. This differs from the screenshot's speculation that only the stop/backoff elapsed-time assertion was sensitive. Add explicit synchronization/readiness on sent frames/count, without weakening behavior or merely increasing deadlines. The screenshot disclosed earlier nonreproducible failures; differing results do not establish fabricated testing.

## Additional deterministic lifecycle probes

1. **Late open leaves an old connection alive.** Fake the actual SDK window where `connect()` has not created its WebSocketApp yet, so `close_connection()` is a no-op. Let both stop joins expire, start a replacement, then allow the old attempt to create/open its app. Its callback is ignored, but it is not closed. Probe result: two live connections, old thread still alive, two ineffective close requests. Capturing generation IDs protects shared state but does not reap a late-opening retired SDK client. This agrees with the disclosed gap but is not harmless under the single-connection/shutdown requirement.
2. **In-flight old data escapes the entry-only check.** Let an old data callback pass its generation check, block inside parsing, replace the connection, then resume parsing. The old price 90.0 is delivered with generation 2 current and zero stale drops. The guard must cover publication/delivery relative to retirement, not only entry into `_on_data`.

The simpler delayed-callback and late-finalizer cases are substantially improved. No new behavior flag is supported: these are implementation/test gaps, and the response discloses a late-connect limitation and intermittent failures. A successful run or discarded callbacks do not prove all lifecycle requirements, so the next correction should close these concrete gaps rather than accept them as complete.

Next prompt: reap late-opening retired clients, guard in-flight tick publication, and make the two flaky readiness assertions deterministic. Bounded session-expiry waiting, silent-connection health monitoring and periodic pending-send retry remain later agreed work; they are not added requirements invented by this review.

Evidence: `.codex-attachments/task-b-check5-review/` contains workspace metadata, independent test output, deterministic generation-edge probe and results. No broker or database connection was made; SDK behavior was modeled with controlled fake sockets and real manager threads.
