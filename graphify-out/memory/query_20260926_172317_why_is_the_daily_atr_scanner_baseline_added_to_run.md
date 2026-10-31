---
type: "query"
date: "2026-09-26T17:23:17.023058+00:00"
question: "Why is the daily ATR scanner baseline added to Run 16, should it govern the selected filters, and why is its return far higher?"
contributor: "graphify"
source_nodes: ["EnhancedMomentumBacktestPanel()", "ProfileBacktester", "EnhancedMomentumBacktestRequest"]
---

# Q: Why is the daily ATR scanner baseline added to Run 16, should it govern the selected filters, and why is its return far higher?

## Answer

Code inspection shows the 1d scanner/ATR replay is inserted unconditionally as a separate comparison row before selected presets, whose own 5m execution and daily-context expressions are preserved. The likely benchmark purpose is an inference; no documented author rationale was found. Run 16's +29.56% is not trustworthy: candles_1d contains multiple rows per stock/date with mixed price scales, and the replay processes those rows as separate daily bars. The filter loader also feeds all daily rows into point-in-time context, so same-session daily data can leak into earlier 5m bars. Normalize daily candles to one consistent corporate-action-adjusted row per exchange session and only expose completed daily bars, then rerun before comparing.

## Source Nodes

- EnhancedMomentumBacktestPanel()
- ProfileBacktester
- EnhancedMomentumBacktestRequest