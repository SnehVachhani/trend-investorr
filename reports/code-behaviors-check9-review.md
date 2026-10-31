# Check 9 final review

The cumulative Check 9 export was reconstructed over the AlgoAnalyst starter ZIP in a temporary folder; the working project was not changed.

All trading tests were independently run: **247 passed in 11.32s**. This is not a full-project pass claim. The separate acceptance command and output are recorded in `.codex-attachments/check9-review/acceptance-tests.log`.

The earlier independent malformed-response, position-lag, prior-day, and shared-capital probes were repeated. Duplicate submissions stayed blocked; combined estimated exposure was ₹60,000 against the ₹60,000 cap.

The lock-timeout probe was also repeated. With a BUY request in flight, square-off now reports not settled, confirmation does not report confirmed flat, and one unresolved entry remains visible. The delayed BUY can still be accepted; this is reported as a pending operational risk rather than successful square-off.

Material boundaries remain: shared-store protection is local to participating instances using that directory; an entry that fills after the final square-off snapshot is alerted and requires operator action or another permitted square-off trigger; cancellation requests are not confirmed until broker evidence arrives. UNKNOWN outcomes have their own counter and do not independently trigger the circuit breaker under the new policy. Paper state remains per instance.

No additional implementation defect was established by these final probes. A final combined acceptance run and whole-task handoff summary in Taiga should supply the reviewer evidence. The screenshot discloses full-project failures, so neither this review nor the final brief should claim the entire project suite is green.

## Follow-up audit against the exact task brief

An additional identity probe found a material remaining defect after the initial review. An UNKNOWN NSE BUY with a hidden broker row can adopt an unrelated cancelled BSE order carrying the same order tag. `_reconcile_reservation` matches that recovery by tag alone, resolves the NSE reservation against the BSE terminal state, and allows a second NSE submission. The independently run probe produced two NSE submissions while the original remained hidden. Evidence: `.codex-attachments/check9-review/identity-probe.json`.

This violates the stated requirement to establish the outcome of an unclear submission before allowing another order. Tag recovery must validate instrument identity, exchange, product, and side, and handle ambiguous matches conservatively. The earlier statement that no further defect had been established is superseded by this probe. The task is not ready for an unqualified completion claim.

The exact task wording does not independently require UNKNOWN outcomes to trip a circuit breaker. That policy should be disclosed, but it is not the new acceptance blocker. Restart and signal-to-trade preservation were also independently checked: 43 passed in 3.12s. The final Done when still needs its actual new test paths and final observed result once the remaining identity defect is fixed.
