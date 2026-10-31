---
type: "query"
date: "2026-09-24T14:42:16.739316+00:00"
question: "Does the codebase store stock sector or industry, and does Upstox provide and persist sector details?"
contributor: "graphify"
source_nodes: ["StockEntity", "SectorEntity", "SectorClassifier"]
---

# Q: Does the codebase store stock sector or industry, and does Upstox provide and persist sector details?

## Answer

Yes, sector schemas exist in the stock master and Upstox profile store. Upstox profile normalization reads data.sector and persists it in company_fundamental_profiles, with raw payload snapshots too. The stock-master sector_id and industry fields are separate and are not populated by the current Angel One instrument loader. Current sector scoring reports unavailable.

## Source Nodes

- StockEntity
- SectorEntity
- SectorClassifier