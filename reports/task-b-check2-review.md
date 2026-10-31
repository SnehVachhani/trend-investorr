# Task B, Check 2 review

Reviewed cumulative export `f1dbbcd4-1689-4182-8641-125ec53b0e35_2_a_changed_files` against the shared backend starter in an isolated reconstruction. Two files changed: the manager and subscription test module. Active source was not synchronized.

## Independent selected result

The subscription and live-candle test files produced **38 passed**: 11 subscription tests and 27 unchanged live-candle checks. The callback fixture now exercises the installed SDK's one-argument close callback; the optional extra close arguments preserve compatibility with direct websocket-client style calls. The installed SDK source was inspected locally: its user close hook receives one argument and its internal resubscribe path can bypass the user's open callback. The model discloses its sandbox's SDK-install limitation rather than claiming an installed-version test it did not perform.

Requested versus sent state, failed subscribe/unsubscribe retention, reconnect reversals and non-acknowledgment terminology are improved. This is an incremental fix, not completion of the full Task B scope. No new behavior flag is supported by the response.

## Reproduced capacity gaps for the next step

- With project per-connection capacity 1000, requesting 1001 distinct tokens is accepted. The manager still multiplies that limit by the maximum three connections while constructing only one connection.
- Start with 1000 sent tokens, fail every unsubscribe send, cancel one requested token, then request a replacement token. Requested count is 1000, but sent count becomes 1001 because `_sync_locked` sends additions even when an earlier removal failed. The fake socket records sends, not broker acceptance; this demonstrates over-capacity requests, not a claim that the broker accepts 1001 subscriptions.

Correct capacity for the actually implemented single connection, retain atomic requested-state rejection, and prevent replacement additions from consuming slots still occupied by failed unsubscribe sends. Pending/duplicate/offline requests must count consistently.

Later required work remains SDK reconnect ownership, generation-safe callbacks, bounded clean-close/error recovery, stop/restart, periodic reconciliation of failed changes, and silent connection/heartbeat health monitoring. Do not demand those all be finished in this incremental bookkeeping response. Optional stale-tick filtering is not automatically a new acceptance condition unless needed for the agreed callback/lifecycle behavior.

Evidence: `.codex-attachments/task-b-check2-review/` contains workspace metadata, independent test output, capacity probes and results. Screenshotted extra suite counts were not independently rerun.
