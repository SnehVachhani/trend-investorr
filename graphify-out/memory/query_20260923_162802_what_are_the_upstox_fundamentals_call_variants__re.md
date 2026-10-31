---
type: "query"
date: "2026-09-23T16:28:02.465928+00:00"
question: "What are the Upstox fundamentals call variants, refresh cadences, and safe batch timing in this codebase?"
contributor: "graphify"
source_nodes: ["FundamentalsSyncService", "UpstoxFundamentalsClient", "FundamentalStore"]
---

# Q: What are the Upstox fundamentals call variants, refresh cadences, and safe batch timing in this codebase?

## Answer

The Upstox published limits for other standard APIs are 50 requests/second, 500/minute, and 2,000/30 minutes per API/user. Public docs do not specify fixed versus sliding window semantics or timer anchor, and do not classify Fundamentals separately. Current code uses 450/minute, 1,500 logical endpoint jobs per batch, 12 batches/day, and a 30-minute sleep after sync_due completes; retries are extra HTTP attempts not counted by the logical batch counter. The service has 13 variants across 8 route families: daily key_ratios and corporate_actions (2); every 7 days four income variants, two balance sheets, two cash flows, share_holdings (9); every 30 days profile and competitors (2). With 4,030 mapped listing rows, approximate due counts are 8,060 daily, 36,270 weekly, and 8,060 monthly; average steady-state weekday demand is about 15,700. Same-ISIN NSE series are not deduplicated because due state is keyed per stock_id. Quarterly income requests with fs=true include annual full_statement detail, making yearly variants a candidate for removal if annual summary/change fields are not required. Current persistence writes raw payload, normalized data, and sync state from one HTTP response. No public bulk Fundamentals endpoint is documented; routes take one ISIN.

## Source Nodes

- FundamentalsSyncService
- UpstoxFundamentalsClient
- FundamentalStore