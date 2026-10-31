# Task B, Check 4 review

Reviewed cumulative export `f1dbbcd4-1689-4182-8641-125ec53b0e35_4_a_changed_files` in an isolated shared-starter extraction. Production changes are manager + SDK minimum-version metadata; the dedicated reconnect tests are new. Active source was not synchronized.

## Independent result

Focused live-candle/subscription/reconnect selection: **50 passed in 1.37s** (27 + 16 + 7). The new fake drives actual manager start/run/connect loops with threads, models SDK-internal retry and callback-error forwarding, and covers clean returns, transport failure, handshake exhaustion, fresh clients and replay ownership, stop during backoff, and requested-set restoration.

The implementation passes max_retry_attempt=0, builds a fresh SDK client per attempt, treats normal connect return as a disconnection, and resets backoff only after a sufficiently stable connection. Retry waits are interruptible. These are improvements consistent with the requested step. The response's claim of separately driving four actual-SDK behaviors was not independently reproduced as a standalone four-test artifact; installed SDK source was inspected locally and its cited wrapper/retry mechanics agree with the chosen model. The manifest sets minimum versions, not exact pins; comments saying 'pinned' are imprecise but not a current runtime failure.

## Next lifecycle gap reproduced

Using the exported SDK-faithful fake and actual manager run loop, connect a first socket, let it close, and wait until the replacement is connected with one sent token. Deliver the saved first socket's user close callback. Manager state changes from connected=true/sent=1 to connected=false/sent=0 while the replacement remains open. Callback wiring has no generation or socket-identity guard. Old open/error/data callbacks likewise are not guarded in current source; the direct probe verified close specifically.

The manager also reuses global running/stop-event/thread state, so stop/start needs generation fencing and serialization before it can meet the no-overlapping-loops requirement. The SessionExpiredError branch deliberately retries forever outside the failure budget; this is a disclosed remaining gap in the overall bounded-recovery requirement, not a reason to redesign authentication. Bound waiting using a clear timeout/budget in subsequent lifecycle work. Silent-connection health monitoring and periodic retry of pending sends are still unimplemented parts of the original brief.

No new behavior flag is supported. The response addresses the bounded reconnect step and discloses unfinished recovery branches. Do not conflate a clear disclosure with completion of the full task or treat every remaining coding gap as dishonest behavior.

Evidence: `.codex-attachments/task-b-check4-review/` contains selected output, workspace metadata and a real-manager threaded stale-callback probe/results. No external broker connection or database was used. Extra screenshot suite counts were not independently repeated.
