# Task B, Check 6 review

Reviewed cumulative export `f1dbbcd4-1689-4182-8641-125ec53b0e35_6_a_changed_files` in an isolated reconstruction of the shared starter. Active source was not synchronized.

## Independent verification

The four focused files produced **60 passed in 2.75s** (27 live-candle, 16 subscription, seven reconnect, ten generation tests), with no failures or skips in that selection. Screenshot repetition/full-sweep totals were not independently repeated.

Repeated the two earlier deterministic generation probes:

- Delayed WebSocketApp creation after stop/restart: the retired client receives a third close request on its late open; after waiting for its cleanup, only one live connection remains and the old thread has exited.
- Data handler paused in parsing while its socket is replaced: no stale price is delivered and stale drop count becomes one. The current generation stays connected.

The callback closure now captures its owning SDK client so a stale late-open path can close the correct object outside the state lock. A pre-connect run check avoids opening if stop already retired the run. Tick parsing remains unlocked; buffering/delivery is guarded by a delivery lock with a generation recheck, and normal retirement acquires that lock before the subscription lock. The tests now wait for `is_synced`, a lock-protected connected/requested-equals-sent condition, rather than reading frames at the instant `is_connected` changes. This addresses the readiness failures independently observed in Check 5 without weakening their frame assertions.

## Assessment and remaining agreed scope

The reproduced late-open, stale-parsing and test-readiness issues are corrected. No new behavior flag is supported; the model also explicitly corrected an invalid test assumption about the blocking readiness getter.

Remaining original requirements: silent/unresponsive connection detection using verified SDK heartbeat/connection evidence, automatic retry of pending subscription changes while an otherwise healthy connection remains open, bounded waiting on SessionExpiredError, and combined lifecycle verification after those are implemented. Slow application callbacks can delay retirement because delivery is synchronous; the task does not require moving consumers to a new execution architecture, but health/stop work must not create an additional reconnect owner or let old monitoring mutate current generations.

Next prompt should add generation-bound connection-health monitoring and healthy-connection pending-send reconciliation, with controlled-clock tests for quiet healthy sockets, stalled heartbeats, retry exhaustion and monitor shutdown. Session-expiry budget can be handled as the subsequent bounded-recovery step without redesigning authentication.

Evidence: `.codex-attachments/task-b-check6-review/` contains workspace metadata, focused output and repeated generation-edge probes/results. No external broker or PostgreSQL was used.
