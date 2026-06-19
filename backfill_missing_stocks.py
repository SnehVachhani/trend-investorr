"""
Backfill candle data for stocks that have zero candles in candles_1d.
Targets non-EQ series stocks (BE, SM, ST, BZ) plus any EQ stocks
that are missing data.

Usage:
    python backfill_missing_stocks.py              # backfill all missing
    python backfill_missing_stocks.py --series BE   # backfill only BE series
    python backfill_missing_stocks.py --limit 50    # backfill first 50 missing
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from loguru import logger

# Logging
logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>", level="INFO")

from config.settings import Settings
from storage.database import Database
from storage.models import CandleStore, CandleCoverageStore, StockStore
from data.fetchers.historical_fetcher import HistoricalFetcher
from data.data_ingestion import DataIngestionService
from data.auth.smartapi_auth import SmartAPISession
from core.security.vault import SecureVault
from core.security.audit import AuditTrail
from core.constants import CandleInterval


def main():
    parser = argparse.ArgumentParser(description="Backfill missing stock candles")
    parser.add_argument("--series", type=str, default=None, help="Filter by series (BE, SM, ST, BZ)")
    parser.add_argument("--limit", type=int, default=0, help="Max stocks to process (0 = all)")
    parser.add_argument("--interval", type=str, default="all", help="Interval: 1d, 5m, 15m, 1h, all")
    parser.add_argument("--dry-run", action="store_true", help="List stocks only, don't fetch")
    args = parser.parse_args()

    # ── Init ──
    settings = Settings()
    db = Database(
        host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.DB_NAME,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        min_connections=1, max_connections=3,
    )
    db.connect()

    # Find stocks with no candle data
    series_filter = ""
    params: list = []
    if args.series:
        series_filter = "AND s.series = %s"
        params.append(args.series.upper())

    limit_clause = ""
    if args.limit > 0:
        limit_clause = f"LIMIT {args.limit}"

    # Determine which candle table to check for "missing"
    table_map = {"1d": "candles_1d", "5m": "candles_5m", "15m": "candles_15m", "1h": "candles_1h", "1m": "candles_1m"}
    check_table = table_map.get(args.interval, "candles_1d")
    if args.interval == "all":
        check_table = "candles_1d"  # For "all" mode, use 1d as the reference

    missing_stocks = db.fetch_all(f"""
        SELECT s.id, s.symbol, s.token, s.series
        FROM stocks s
        WHERE s.is_active = TRUE AND s.exchange = 'NSE'
          AND NOT EXISTS (SELECT 1 FROM {check_table} WHERE stock_id = s.id)
          {series_filter}
        ORDER BY s.series, s.symbol
        {limit_clause}
    """, tuple(params) if params else None)

    logger.info(f"Found {len(missing_stocks)} stocks with no data in {check_table}")

    if not missing_stocks:
        logger.info("All stocks have candle data! Nothing to do.")
        db.disconnect()
        return

    # Show summary by series
    from collections import Counter
    series_counts = Counter(s["series"] for s in missing_stocks)
    for series, count in sorted(series_counts.items()):
        logger.info(f"  {series}: {count} stocks missing candles")

    if args.dry_run:
        logger.info("DRY RUN — listing stocks only:")
        for s in missing_stocks[:30]:
            logger.info(f"  {s['symbol']:30s} series={s['series']}  token={s['token']}")
        if len(missing_stocks) > 30:
            logger.info(f"  ... and {len(missing_stocks) - 30} more")
        db.disconnect()
        return

    # ── Login to SmartAPI ──
    vault = SecureVault(vault_path=settings.vault_dir)
    audit = AuditTrail(log_dir=settings.audit_dir)
    session = SmartAPISession(vault=vault, audit=audit)
    session.login()

    fetcher = HistoricalFetcher(session=session)
    candle_store = CandleStore(db)
    stock_store = StockStore(db)
    coverage_store = CandleCoverageStore(db)

    ingestion = DataIngestionService(
        historical_fetcher=fetcher,
        candle_store=candle_store,
        stock_store=stock_store,
        coverage_store=coverage_store,
    )

    # Determine intervals
    if args.interval == "all":
        intervals = [
            CandleInterval.ONE_DAY,
            CandleInterval.FIVE_MINUTE,
            CandleInterval.FIFTEEN_MINUTE,
            CandleInterval.ONE_HOUR,
        ]
    else:
        interval_map = {
            "1d": CandleInterval.ONE_DAY,
            "5m": CandleInterval.FIVE_MINUTE,
            "15m": CandleInterval.FIFTEEN_MINUTE,
            "1h": CandleInterval.ONE_HOUR,
            "1m": CandleInterval.ONE_MINUTE,
        }
        iv = interval_map.get(args.interval)
        if not iv:
            logger.error(f"Unknown interval: {args.interval}")
            db.disconnect()
            return
        intervals = [iv]

    # ── Backfill each stock ──
    total_candles = 0
    total_errors = 0
    start_time = time.time()

    for i, stock in enumerate(missing_stocks, 1):
        logger.info(f"[{i}/{len(missing_stocks)}] Backfilling {stock['symbol']} (series={stock['series']})")
        try:
            stats = ingestion.backfill_stock(
                stock={"id": stock["id"], "symbol": stock["symbol"], "token": stock["token"]},
                intervals=intervals,
            )
            total_candles += stats["candles_stored"]
            total_errors += stats["errors"]

            if stats["candles_stored"] > 0:
                logger.info(f"  ✓ {stats['candles_stored']} candles stored")
            else:
                logger.warning(f"  ⚠ No candle data returned from API")

        except Exception as e:
            total_errors += 1
            logger.error(f"  ✗ Failed: {e}")

        # Rate limit — 0.3s between stocks to avoid hitting API limits
        time.sleep(0.3)

        # Progress update every 50 stocks
        if i % 50 == 0:
            elapsed = time.time() - start_time
            rate = i / elapsed * 60
            logger.info(f"  Progress: {i}/{len(missing_stocks)} stocks | {total_candles:,} candles | {rate:.0f} stocks/min")

    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info(f"BACKFILL COMPLETE")
    logger.info(f"  Stocks processed: {len(missing_stocks)}")
    logger.info(f"  Total candles:    {total_candles:,}")
    logger.info(f"  Errors:           {total_errors}")
    logger.info(f"  Time:             {elapsed:.1f}s ({elapsed/60:.1f}min)")
    logger.info("=" * 60)

    db.disconnect()


if __name__ == "__main__":
    main()
