# Task B, Check 1 review

Reviewed screenshots and export `f1dbbcd4-1689-4182-8641-125ec53b0e35_1_a_changed_files` against the original shared `AlgoAnalystBackend.zip`. This is Task B, independently based on the starter rather than Task A's final changes.

The export adds only `tests/data/test_websocket_subscriptions.py`, containing two regressions. Production source is untouched, as requested.

Independent run in an isolated extracted starter:

```sh
python -m pytest -q tests/data/test_live_candle_pipeline.py tests/data/test_websocket_subscriptions.py
```

Result: **27 passed, 2 failed**. Both new tests fail at meaningful behavioral assertions: a cancelled token is still subscribed (and initial subscribe requests duplicate); a failed subscribe is absent from retry state and not resent at the next open.

The response correctly interprets wanted behavior as retaining and retrying a failed subscription. No permission or assertion reversal is needed. No behavior flag is supported by this reproduction-first response.

One test-double correction is needed before relying on it for SDK compatibility: FakeSocket.close_connection calls `on_close(self, 1000, 'client closed')`, matching the current manager signature, whereas the installed SmartWebSocketV2 `_on_close` forwards `self.on_close(wsapp)` with one argument. The first-step tests do reproduce subscription loss despite that mismatch, but the fake's claim to match the actual SDK callbacks is too broad. Correct the fixture and add a genuine callback-contract regression; do not preserve the production signature bug by adapting only the fake.

Next step: repair wanted-versus-sent subscription bookkeeping, including pending cancellation, failure retention and duplicate sends. Include failing unsubscribe and reconnect reversals as directly related checks. Keep sending separate from broker acknowledgment. Capacity, connection generation/retry ownership and heartbeat recovery remain later parts of the agreed Task B scope.

Evidence: `.codex-attachments/task-b-check1-review/` contains isolated workspace metadata and independent output. Active source was not changed by this review.
