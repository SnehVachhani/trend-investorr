---
type: "query"
date: "2026-09-20T21:04:33.524984+00:00"
question: "How can watchlist analysis use more of the available CPU?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: How can watchlist analysis use more of the available CPU?

## Answer

The process-pool cap was raised from five to eight worker processes. The FastAPI service was restarted successfully, so subsequent watchlist analyses can schedule up to eight independent stock batches, subject to the number of selected stocks.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx