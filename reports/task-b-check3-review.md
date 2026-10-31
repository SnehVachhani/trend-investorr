# Task B, Check 3 review

Reviewed cumulative export `f1dbbcd4-1689-4182-8641-125ec53b0e35_3_a_changed_files` in an isolated shared-starter reconstruction. Runtime changes remain confined to the WebSocket manager, with the dedicated subscription test module. Active application source was not changed.

Independent focused selection: **43 passed in 1.39s** (16 subscription tests + 27 existing live-candle tests).

The implementation now uses the per-connection capacity rather than multiplying by the maximum connection count. Requested distinct tokens, including offline/unsent requests, are validated before requested-state mutation. `_sync_locked()` attempts removals first and only sends additions into the remaining sent-request capacity. A failed unsubscribe therefore retains its occupied slot and defers a replacement; a subsequent successful removal frees it. The expanded fake tracks live/peak-live state and the added tests assert boundary, overflow atomicity, duplicate requests, pending/sent accounting and failed-removal replacement behavior.

No new behavior flag is supported. The disclosed lack of automatic retry on a healthy unchanged connection and the SDK/lifecycle gaps remain required later work, not proof that this bounded capacity correction was misrepresented. Arbitrary sorted admission of pending additions is acceptable because the task specifies no token-priority policy.

Next implementation area: inspect the actual installed SDK and choose clear ownership of reconnect/resubscribe. Current manager `_run()` can reconnect immediately after a clean return and reset its retry count without a stable healthy connection. The SDK also contains internal retry/resubscribe behavior; its resubscribe `_on_open` branch bypasses the user's hook, and it mutates its recorded subscription list before a send can raise. Local source inspection confirms these are real SDK mechanics, but their manager-level effects still need faithful regressions and a coherent integration. Stop/restart generation fencing and heartbeat recovery follow as additional agreed scope.

Evidence: `.codex-attachments/task-b-check3-review/` contains reconstructed workspace metadata and selected output. Extra screenshot suite counts and pre-fix runs were not independently repeated. No PostgreSQL was required.
