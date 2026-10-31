---
type: "query"
date: "2026-09-20T21:54:22.525855+00:00"
question: "How does five-minute candle history affect the runtime of watchlist filter analysis, including data retrieval, strategy evaluation, and the eight-worker scheduler?"
contributor: "graphify"
source_nodes: ["Watchlist", "Candle", "FilterBacktestPanel.tsx"]
---

# Q: How does five-minute candle history affect the runtime of watchlist filter analysis, including data retrieval, strategy evaluation, and the eight-worker scheduler?

## Answer

Curated presets execute on five-minute bars and load point-in-time daily context. Each filter batch loads candles once, builds the atomic cache once, then evaluates all 11 presets sequentially for that stock; eight stock batches run concurrently. Runtime is therefore approximately linear in the requested five-minute bars. The current loader fetches every candle through the end date, so adding older history would also slow one-year runs unless it is constrained to the selected interval plus the required 60 five-minute and 205 daily warm-up candles.

## Source Nodes

- Watchlist
- Candle
- FilterBacktestPanel.tsx