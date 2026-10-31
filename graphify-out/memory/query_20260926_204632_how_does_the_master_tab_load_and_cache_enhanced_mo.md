---
type: "query"
date: "2026-09-26T20:46:32.110638+00:00"
question: "How does the Master tab load and cache enhanced momentum candidates, daily setup signals, and fundamental metrics?"
contributor: "graphify"
source_nodes: ["Scanner"]
---

# Q: How does the Master tab load and cache enhanced momentum candidates, daily setup signals, and fundamental metrics?

## Answer

Direct source inspection shows the Master panel reads the saved Enhanced Momentum scan snapshot from browser localStorage and computes visible filtering and sorting locally. Before caching was added, each Master mount requested recent daily candles from POST /api/enhanced-momentum-scanner/setup-candles, classified those candles locally with detectMasterSetups, and requested fundamentals from POST /api/stocks/fundamentals/score-inputs. The Master panel now stores the derived setup signals and fundamental metrics in browser localStorage, keyed by the completed Enhanced Momentum scan task id plus as-of date, candidate ids, and sectors. Reopening with the same scan reuses the cached results; a new Enhanced Momentum scan changes the key and reloads them, while Recheck setups forces only the setup candle refresh. The full Enhanced Momentum scan itself runs only when explicitly started or resumed from a persisted running task.

## Source Nodes

- Scanner