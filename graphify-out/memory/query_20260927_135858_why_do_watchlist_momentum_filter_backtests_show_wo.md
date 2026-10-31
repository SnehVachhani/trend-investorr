---
type: "query"
date: "2026-09-27T13:58:58.083279+00:00"
question: "Why do watchlist momentum filter backtests show worse returns than earlier saved results after E2E was re-added?"
contributor: "graphify"
source_nodes: ["Watchlist", "date"]
---

# Q: Why do watchlist momentum filter backtests show worse returns than earlier saved results after E2E was re-added?

## Answer

The existing graph traversal found the Watchlist type at frontend/src/lib/types.ts:204 and several date-related helpers, but no edges for watchlist analysis, saved filter runs, capital allocation, candle timestamps, or charges. The graph is insufficient for this diagnostic; direct source and database inspection are required.

## Source Nodes

- Watchlist
- date