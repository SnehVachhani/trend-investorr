# Shared Python starter for independent backend tasks

Archive: `/Users/macbookpro/Downloads/AlgoAnalystBackend.zip`
Root folder: `AlgoAnalystBackend`
Files: 169
Archive bytes: 637778
SHA-256: `f65bc08a3d811c8a0bca88731f855b730f4d0e71651e67f1f8166bd28d62ea08`

Both the backtest job-recovery task and the live-feed lifecycle task start from this same archive in separate workspaces. Neither requested fix is implemented in the starter. Task A is now backend-only: API persistence/recovery and compatibility with the existing client response shape; no required frontend code, npm commands or browser test suite. Task B keeps its socket-lifecycle scope.

All packaged Python sources match the active checkout, including all 12 completed Task 4 files. Packaging changes are limited to README, ignore rules and API dependencies in runtime/development manifests. Frontend and Java code, Git history, agent instructions, environments, logs, caches, runtime data and local credential files are excluded. A scan for common key/JWT/private-key/email/personal-path patterns returned no findings. Synthetic security fixtures remain runnable.

The archive was extracted into a separate temporary directory. Both manifests were installed into a new Python 3.12 environment; the checks ran from the extracted workspace, not the active checkout:

- Live-candle baseline: 27 passed in 1.67s.
- Existing Enhanced Momentum replay checks: 8 passed, 2 failed in 1.83s. The same missing-helper patch and UTC-versus-IST expectations found in the earlier baseline remain; the starter does not silently repair them.
- Completed Task 4 acceptance selection: 236 passed in 11.79s.

Evidence: `.codex-attachments/shared-starter-20261029/`, including source hashes, exclusion/scan metadata, installation log, test logs and extracted-workspace location. The full repository test suite was not run.

Use the same uploaded ZIP for both sessions, retaining its hash as the common base. Merge each final patch relative to this base; do not overwrite one session's cumulative folder with the other. Run the union of relevant checks after merging. Each backend task must pass its own acceptance checks without the other task's implementation.
