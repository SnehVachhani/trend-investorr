---
type: "query"
date: "2026-09-20T21:42:37.280387+00:00"
question: "Which fields feed the View stocks leaderboard modal, and where can all displayed columns be made sortable without changing watchlist analysis results?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: Which fields feed the View stocks leaderboard modal, and where can all displayed columns be made sortable without changing watchlist analysis results?

## Answer

The View stocks modal derives each row from job.stock_results and the selected filter combination. Sorting can stay entirely in WatchlistBacktestPanel: stock symbol/name, behaviour label, 1Y move, trend efficiency, filter return, drawdown, trade count, status, and trade-log availability are ordered in the frontend view only. No backtest calculation, leaderboard metric, or persisted result changes.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx