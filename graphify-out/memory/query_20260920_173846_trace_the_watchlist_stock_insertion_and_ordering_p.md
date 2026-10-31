---
type: "query"
date: "2026-09-20T17:38:46.948906+00:00"
question: "Trace the watchlist stock insertion and ordering path, including the watchlist page and API endpoint."
contributor: "graphify"
source_nodes: ["Watchlist", "api_ts", "page_tsx"]
---

# Q: Trace the watchlist stock insertion and ordering path, including the watchlist page and API endpoint.

## Answer

The watchlist page uses the API client to add stocks and reload the saved list. The API exposes watchlist stock retrieval and the list is ordered by stored priority. New additions must receive a priority above the current maximum to appear first.

## Source Nodes

- Watchlist
- api_ts
- page_tsx