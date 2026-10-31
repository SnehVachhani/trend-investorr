---
type: "query"
date: "2026-09-20T20:29:38.058863+00:00"
question: "How long did the completed IMPS watchlist analysis take, and how much faster was it than the prior implementation?"
contributor: "graphify"
source_nodes: ["Watchlist", "FilterBacktestPanel.tsx"]
---

# Q: How long did the completed IMPS watchlist analysis take, and how much faster was it than the prior implementation?

## Answer

The IMPS job ran from 2026-09-20T20:10:56.888472+00:00 to 2026-09-20T20:18:46.555871+00:00: 469.7 seconds (7m49.7s) for 50 stocks times 11 presets = 550 backtests. It completed 549 successfully; GNA-EQ Weinstein had a cache/reference divergence caught by the safeguard. The prior Best run took 1,283.7 seconds for 352 backtests. Throughput improved from 16.5 to 70.3 backtests/minute (4.27x); a 550-backtest run at the old rate was about 33m26s. Added a transparent reference-path restart on a parity mismatch so future jobs do not become partial.

## Source Nodes

- Watchlist
- FilterBacktestPanel.tsx