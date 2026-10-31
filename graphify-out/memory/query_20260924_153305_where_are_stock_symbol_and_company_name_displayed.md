---
type: "query"
date: "2026-09-24T15:33:05.511662+00:00"
question: "Where are stock symbol and company name displayed in scanners, watchlists and other stock lists?"
contributor: "graphify"
source_nodes: ["api.ts", "types.ts", "StockDetail"]
---

# Q: Where are stock symbol and company name displayed in scanners, watchlists and other stock lists?

## Answer

The API now resolves official company names from the local company directory for momentum and enhanced momentum results, stock search/details, watchlist analysis, signals, orders and positions. Shared StockIdentity renders the ticker and the distinct company name separately; duplicate ticker-as-name values are suppressed, and saved scanner results are hydrated through a batch name lookup.

## Source Nodes

- api.ts
- types.ts
- StockDetail