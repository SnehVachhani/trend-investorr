---
type: "query"
date: "2026-09-23T15:58:39.428402+00:00"
question: "How long does a full Upstox fundamentals synchronization take under the configured rate limits?"
contributor: "graphify"
source_nodes: ["FundamentalsSyncService", "UpstoxFundamentalsClient"]
---

# Q: How long does a full Upstox fundamentals synchronization take under the configured rate limits?

## Answer

The initial fundamentals backfill is estimated at 52,390 requests (about 4,030 mapped listings × 13 endpoint variants). The app paces at 450 requests/minute, caps each batch at 1,500 requests, and runs at most 12 batches per day with 30-minute waits between batches: 18,000 logical requests/day, or 35 batches total. In ideal conditions with no retries, this takes three weekday sync runs. Request transmission alone is about 1 hour 57 minutes; the configured batch waits make the three scheduled run windows total about 18 hours, distributed across the three days. Upstox's published standard API limits are 50/second, 500/minute, and 2,000 per 30 minutes, enforced per API and user; the Fundamentals page does not list a separate category, so the app treats calls conservatively as one shared workload.

## Source Nodes

- FundamentalsSyncService
- UpstoxFundamentalsClient