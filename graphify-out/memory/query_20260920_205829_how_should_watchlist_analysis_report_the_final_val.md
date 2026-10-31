---
type: "query"
date: "2026-09-20T20:58:29.193686+00:00"
question: "How should watchlist analysis report the final value of an equal-weight portfolio?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: How should watchlist analysis report the final value of an equal-weight portfolio?

## Answer

Watchlist analysis now treats the submitted capital as one equal-weight portfolio fund. The API divides it across selected stocks for independent simulations, retains allocations for unavailable results as cash, and reports each filter's final portfolio value in the leaderboard. The default is ₹10,00,000.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx