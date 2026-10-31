---
type: "query"
date: "2026-09-26T17:10:15.182825+00:00"
question: "How does the automatic Enhanced Momentum daily ATR baseline work, and why is it included in each replay?"
contributor: "graphify"
source_nodes: ["EnhancedMomentumBacktestPanel()"]
---

# Q: How does the automatic Enhanced Momentum daily ATR baseline work, and why is it included in each replay?

## Answer

The graph contains no recorded author rationale for including the baseline. In the current implementation, the replay always adds a scanner-only daily ATR strategy before it appends each selected Watchlist Analysis preset, so N selected filters produce N+1 strategy rows. Its functional role appears to be a screen-only reference comparison, but that purpose is an inference rather than a documented decision. For Run 16, the scanner refreshes monthly; eligible matches can enter at the next daily open when a shared capital slot is free. A 20-day ATR sets the target and stop distances, which are checked against daily candles; the trade exits at a hit level or the test end. This baseline is separate from, and not combined with, the five selected filters.

## Source Nodes

- EnhancedMomentumBacktestPanel()