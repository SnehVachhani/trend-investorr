---
type: "query"
date: "2026-09-20T20:36:39.178160+00:00"
question: "Why not increase watchlist backtest CPU workers, and what safe worker count is appropriate on this machine?"
contributor: "graphify"
source_nodes: ["Database", "Watchlist", "ProfileBacktester"]
---

# Q: Why not increase watchlist backtest CPU workers, and what safe worker count is appropriate on this machine?

## Answer

The host has 6 physical CPU cores and 12 logical threads, so the prior four worker processes left two physical cores free, not eight independent cores. PostgreSQL permits 100 connections and had only three open, so database capacity is not the bottleneck. Increased the bounded watchlist analysis process cap from four to five, reserving one physical core for FastAPI, PostgreSQL, and candle synchronization. The change preserves filter/backtest behavior; all 235 tests pass and API health is confirmed.

## Source Nodes

- Database
- Watchlist
- ProfileBacktester