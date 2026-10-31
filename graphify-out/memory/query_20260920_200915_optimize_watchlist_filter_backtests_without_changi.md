---
type: "query"
date: "2026-09-20T20:09:15.411620+00:00"
question: "Optimize watchlist filter backtests without changing any strategy, trade, metric, or response behavior."
contributor: "graphify"
source_nodes: ["Candle", "FilterBacktestPanel.tsx"]
---

# Q: Optimize watchlist filter backtests without changing any strategy, trade, metric, or response behavior.

## Answer

Added a parity-gated vector outcome path for flat cache-backed ALL expressions, retaining the original evaluator for unsupported expressions and reconstructing the original explanation at each actual entry. Changed independent watchlist stock batches from two Python threads to a bounded four-process spawn pool with clean worker database connections. Added exact output parity and scheduler isolation tests; the full suite passes. Local benchmark showed 20.3x faster candle-loop evaluation for a representative 9,000-candle cached expression.

## Source Nodes

- Candle
- FilterBacktestPanel.tsx