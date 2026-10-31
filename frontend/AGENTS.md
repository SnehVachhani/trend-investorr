<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.

## Shared stock details and table behavior

- The enhanced momentum screener's `EnhancedMomentumStockModal` is the canonical stock-details view. Any stock name or symbol shown as an interactive item anywhere in the app—including global search, sectors, scanners, dashboard, watchlists, portfolio, signals, orders, and backtests—must open this same modal. Reuse `StockIdentity` for stock labels and `useStockDetails().openStock(...)` for custom stock controls; do not build a parallel stock modal.
- Every data table shown in the frontend must support sorting by its column headers. The root `SortableTables` enhancement supplies sorting to ordinary tables; keep existing table-specific sorting functional and ensure new custom tables remain sortable across their columns.
<!-- END:nextjs-agent-rules -->
