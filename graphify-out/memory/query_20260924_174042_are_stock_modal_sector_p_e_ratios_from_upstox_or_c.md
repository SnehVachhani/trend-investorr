---
type: "query"
date: "2026-09-24T17:40:42.920089+00:00"
question: "Are stock modal sector P/E ratios from Upstox or calculated by the app?"
contributor: "graphify"
source_nodes: ["UpstoxFundamentalsClient", "FundamentalsSyncService", "FundamentalStore", "getStockFundamentals", "ratioRating"]
---

# Q: Are stock modal sector P/E ratios from Upstox or calculated by the app?

## Answer

The stock modal gets both company and sector ratio values from the Upstox key-ratios endpoint. The app normalizes and stores the supplied company_value and sector_value, and when the modal opens it reads the latest stored snapshot through its backend API rather than making a direct live Upstox call. The app derives the Rating pill by comparing the company ratio with the supplied sector_value; it does not calculate the sector average.

## Source Nodes

- UpstoxFundamentalsClient
- FundamentalsSyncService
- FundamentalStore
- getStockFundamentals
- ratioRating