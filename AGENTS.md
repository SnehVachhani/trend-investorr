# Trend-Investorr project conventions

## Shared stock details and tables

- The enhanced momentum screener's `EnhancedMomentumStockModal` is the canonical stock-details view. Any stock name or symbol shown as an interactive item anywhere in the frontend—including global search, sectors, scanners, dashboard, watchlists, portfolio, signals, orders, and backtests—must open this same modal. Reuse `frontend/src/components/StockIdentity.tsx` for stock labels and `useStockDetails().openStock(...)` for custom stock controls; do not build a parallel stock modal.
- Every data table shown in the frontend must support sorting by its column headers. The shared `SortableTables` enhancement supplies sorting to ordinary tables; keep existing table-specific sorting functional and ensure new custom tables remain sortable across their columns.
