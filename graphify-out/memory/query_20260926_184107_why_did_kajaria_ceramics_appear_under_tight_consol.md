---
type: "query"
date: "2026-09-26T18:41:07.594068+00:00"
question: "Why did Kajaria Ceramics appear under Tight consolidation with momentum score 72.6?"
contributor: "graphify"
source_nodes: ["enhanced_momentum_scanner.py", "MasterScannerPanel.tsx"]
---

# Q: Why did Kajaria Ceramics appear under Tight consolidation with momentum score 72.6?

## Answer

The Master page filters the saved Enhanced Momentum candidate list by an independent daily-chart setup. Tight consolidation matches when the longest 3-8-session window ending at the latest candle has a high/low range <=6%; the tile count is across candidates, while active Master fundamental rules can further narrow table rows. The 72.6 is a weighted composite of cross-sectional percentile subscores: 35% base momentum (20/60/120/252-day returns), 15% relative strength versus Nifty (20/60/120), 25% volatility-adjusted momentum (20/60/120/252), 15% breakout quality (high proximity, participation, close position, extension penalty), 10% market regime; sector weight is currently zero. Default scan filters require score >=55, price >=20, turnover >=₹1Cr, 20D volume ratio >=1.2, nonnegative 20D and 60D returns, >=260 sessions, and SMA20>SMA50; breakout is not required. Screenshot shows +0.6% 20D, +1.4% 60D, 75.2 risk-adjusted score, ATR20 2.52%, volume20 1.28x; its exact other component scores are not shown in Master, so exact numeric decomposition cannot be recovered from the screenshot.

## Source Nodes

- enhanced_momentum_scanner.py
- MasterScannerPanel.tsx