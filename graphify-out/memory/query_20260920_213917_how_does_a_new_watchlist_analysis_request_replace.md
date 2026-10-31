---
type: "query"
date: "2026-09-20T21:39:17.509041+00:00"
question: "How does a new watchlist analysis request replace an existing active analysis without allowing stale worker state or poll responses to overwrite the new request?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: How does a new watchlist analysis request replace an existing active analysis without allowing stale worker state or poll responses to overwrite the new request?

## Answer

The watchlist-analysis POST endpoint now marks any queued or running job as cancelled, removes its registered ProcessPoolExecutor, terminates live child processes, and schedules the new job immediately. The worker checks for cancellation before publishing progress or final results, while the frontend ignores responses whose job ID is no longer current. The UI leaves inputs editable and turns the action into Restart with current settings while an analysis is active.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx