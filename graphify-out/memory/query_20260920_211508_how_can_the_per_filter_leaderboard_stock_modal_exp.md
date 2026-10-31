---
type: "query"
date: "2026-09-20T21:15:08.563690+00:00"
question: "How can the per-filter leaderboard stock modal expose its transaction logs?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: How can the per-filter leaderboard stock modal expose its transaction logs?

## Answer

The per-filter stock modal now includes a View trades button for every completed stock/filter result with a saved run. It reuses the existing trade-detail modal and shows the persisted entries, exits, P&L, and exit reasons.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx