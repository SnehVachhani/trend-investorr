---
type: "query"
date: "2026-09-24T16:40:00.365243+00:00"
question: "How do Enhanced Momentum stock details flow from Upstox into the scanner UI?"
contributor: "graphify"
source_nodes: ["EnhancedMomentumScannerPanel", "EnhancedMomentumStockModal", "FundamentalStore"]
---

# Q: How do Enhanced Momentum stock details flow from Upstox into the scanner UI?

## Answer

The Enhanced Momentum scanner renders the ranked result rows from its scan response. Clicking a symbol opens the stock details modal, which requests GET /api/stocks/{symbol}/fundamentals and candle history. The fundamentals endpoint resolves the stock and reads the latest profile and ratios, ratio history, income/balance-sheet/cash-flow lines, shareholdings, corporate actions, competitors, sync state, and latest raw Upstox payloads from FundamentalStore. The modal computes a coverage-aware fundamental score from profitability, annual growth, balance-sheet and operating-cash-flow signals, valuation multiples, and promoter-holding change; it suppresses the overall score below 50 percent data coverage.

## Source Nodes

- EnhancedMomentumScannerPanel
- EnhancedMomentumStockModal
- FundamentalStore