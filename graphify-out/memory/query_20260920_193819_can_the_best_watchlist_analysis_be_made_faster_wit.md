---
type: "query"
date: "2026-09-20T19:38:19.968000+00:00"
question: "Can the Best watchlist analysis be made faster without reducing the fidelity of the 5-minute backtest?"
contributor: "graphify"
source_nodes: ["Candle", "FilterBacktestPanel.tsx"]
---

# Q: Can the Best watchlist analysis be made faster without reducing the fidelity of the 5-minute backtest?

## Answer

Yes. The analysis path runs a shared candle and atomic-condition cache per stock, then executes each selected preset. The main safe optimizations are compiled boolean signal evaluation, process-based stock parallelism, bounded history reads, leaner persistence, and freshness-aware result reuse. These preserve the 5-minute inputs, date range, metrics, and trade log.

## Source Nodes

- Candle
- FilterBacktestPanel.tsx