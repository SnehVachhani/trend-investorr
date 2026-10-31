"""Run an on-demand Upstox fundamentals sync.

Examples:
    python scripts/sync_fundamentals.py
    python scripts/sync_fundamentals.py --force --limit 25
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings
from core.security.vault import SecureVault
from storage.database import Database
from storage.models import FundamentalStore, StockStore
from data.fundamentals.instrument_resolver import UpstoxInstrumentResolver
from data.fundamentals.service import FundamentalsSyncService
from data.fundamentals.upstox_client import UpstoxFundamentalsClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync due Upstox company fundamentals")
    parser.add_argument("--force", action="store_true", help="refresh all configured endpoints regardless of freshness")
    parser.add_argument("--limit", type=int, default=None, help="maximum stocks in this run")
    parser.add_argument(
        "--all-batches",
        action="store_true",
        help="continue due work in sequential, rate-limited batches until the cycle is drained",
    )
    args = parser.parse_args()
    if args.all_batches and args.force:
        parser.error("--force cannot be combined with --all-batches; forced rows would be fetched repeatedly")

    settings = Settings()
    vault = SecureVault(settings.vault_dir, settings.VAULT_MASTER_KEY)
    vault.setup_from_env(settings)
    token = vault.get_credential("UPSTOX_ANALYTICS_TOKEN") or settings.UPSTOX_ANALYTICS_TOKEN
    if not token:
        parser.error("UPSTOX_ANALYTICS_TOKEN is not configured")

    db = Database(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_connections=settings.db_pool_min,
        max_connections=settings.db_pool_max,
    )
    db.connect()
    try:
        db.run_migrations()
        fundamental_store = FundamentalStore(db)
        service = FundamentalsSyncService(
            client=UpstoxFundamentalsClient(
                token=token,
                base_url=settings.UPSTOX_API_BASE_URL,
                timeout_seconds=settings.UPSTOX_HTTP_TIMEOUT_SECONDS,
                max_retries=settings.UPSTOX_MAX_RETRIES,
                requests_per_minute=settings.UPSTOX_REQUESTS_PER_MINUTE,
                requests_per_30_minutes=settings.UPSTOX_REQUESTS_PER_30_MINUTES,
            ),
            resolver=UpstoxInstrumentResolver(
                cache_dir=settings.data_dir / "upstox_instruments",
                url=settings.UPSTOX_INSTRUMENTS_URL,
            ),
            fundamental_store=fundamental_store,
            stock_store=StockStore(db),
            max_requests_per_run=settings.UPSTOX_MAX_REQUESTS_PER_RUN,
            max_stocks_per_run=args.limit if args.limit is not None else settings.UPSTOX_MAX_STOCKS_PER_RUN,
            profile_refresh_days=settings.UPSTOX_PROFILE_REFRESH_DAYS,
            statement_refresh_days=settings.UPSTOX_STATEMENT_REFRESH_DAYS,
            ratio_refresh_days=settings.UPSTOX_RATIO_REFRESH_DAYS,
            shareholding_refresh_days=settings.UPSTOX_SHAREHOLDING_REFRESH_DAYS,
            corporate_action_refresh_days=settings.UPSTOX_CORPORATE_ACTION_REFRESH_DAYS,
            competitor_refresh_days=settings.UPSTOX_COMPETITOR_REFRESH_DAYS,
        )
        with fundamental_store.sync_cycle_lock(wait=False) as acquired:
            if not acquired:
                print("Another Upstox fundamentals sync cycle is active.", file=sys.stderr)
                return 2
            interval_seconds = max(1, settings.UPSTOX_BATCH_INTERVAL_MINUTES) * 60
            max_batches = max(1, settings.UPSTOX_MAX_BATCHES_PER_CYCLE) if args.all_batches else 1
            stats = None
            for batch_number in range(1, max_batches + 1):
                cooldown = fundamental_store.rate_limit_cooldown_seconds(interval_seconds)
                if cooldown > 0:
                    time.sleep(cooldown)
                stats = service.sync_due(
                    force=args.force,
                    batch_number=batch_number,
                )
                print(json.dumps(stats.__dict__, default=str), flush=True)
                if stats.already_running:
                    print("A fundamentals batch is already recorded as running.", file=sys.stderr)
                    return 2
                attempted = stats.successful_requests + stats.failed_requests
                if (
                    not args.all_batches
                    or stats.pending_requests <= 0
                    or attempted <= 0
                    or batch_number == max_batches
                ):
                    break
                time.sleep(interval_seconds)
        return 0 if stats and stats.failed_requests == 0 and stats.pending_requests == 0 else 2
    finally:
        db.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
