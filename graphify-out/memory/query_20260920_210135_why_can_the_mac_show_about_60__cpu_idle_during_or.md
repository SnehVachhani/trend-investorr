---
type: "query"
date: "2026-09-20T21:01:35.197314+00:00"
question: "Why can the Mac show about 60% CPU idle during or after watchlist analysis?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: Why can the Mac show about 60% CPU idle during or after watchlist analysis?

## Answer

Watchlist analysis is capped at five worker processes on a 12-logical-CPU Mac, so it can use at most about 42% of total logical CPU capacity. The checked 12-stock, 132-backtest job completed in 65.9 seconds and had no remaining analysis workers afterward; idle CPU at that point is expected. Startup candle synchronization is network/rate-limit-bound, so higher CPU concurrency would not make it faster.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx