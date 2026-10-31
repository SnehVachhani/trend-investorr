"""Targeted backfill: intraday candles for all index symbols."""
from config.settings import Settings
from storage.database import Database
from storage.models import CandleStore, CandleCoverageStore
from data.auth.smartapi_auth import SmartAPISession
from data.fetchers.historical_fetcher import HistoricalFetcher
from core.constants import CandleInterval, BACKFILL_DAYS
from core.security.vault import SecureVault
from core.security.audit import AuditTrail
import time

settings = Settings()
db = Database(host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
              user=settings.db_user, password=settings.db_password)
db.connect()

vault = SecureVault(vault_path=settings.vault_dir, master_key=getattr(settings, "VAULT_MASTER_KEY", ""))
audit = AuditTrail(log_dir=settings.audit_dir)
session = SmartAPISession(vault, audit)
session.login()

fetcher = HistoricalFetcher(session)
candle_store = CandleStore(db)
coverage_store = CandleCoverageStore(db)

# Find all index symbols (tokens starting with 999)
indices = db.fetch_all(
    "SELECT id, symbol, name, token FROM stocks WHERE token LIKE %s ORDER BY symbol",
    ('999%',)
)
print(f"Found {len(indices)} index symbols\n")

intervals = [
    (CandleInterval.FIVE_MINUTE, BACKFILL_DAYS[CandleInterval.FIVE_MINUTE]),
    (CandleInterval.FIFTEEN_MINUTE, BACKFILL_DAYS[CandleInterval.FIFTEEN_MINUTE]),
    (CandleInterval.ONE_HOUR, BACKFILL_DAYS[CandleInterval.ONE_HOUR]),
    (CandleInterval.ONE_MINUTE, BACKFILL_DAYS[CandleInterval.ONE_MINUTE]),
]

for interval, days in intervals:
    print(f"\n{'='*60}")
    print(f"  {interval.value} ({days} days)")
    print(f"{'='*60}")
    
    success = 0
    for i, idx in enumerate(indices):
        # Skip if already covered
        existing = db.fetch_one(
            "SELECT candle_count FROM candle_coverage WHERE stock_id=%s AND interval=%s",
            (idx["id"], interval.value)
        )
        if existing and existing["candle_count"] > 0:
            print(f"  [{i+1}/{len(indices)}] {idx['symbol']:25s} SKIP ({existing['candle_count']} candles)")
            continue

        try:
            candles = fetcher.get_candles(
                symbol=idx["symbol"],
                token=idx["token"],
                days=days,
                interval=interval,
            )
            if candles:
                rows = [
                    (idx["id"], c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.vwap)
                    for c in candles
                ]
                candle_store.insert_batch(interval, rows)
                coverage_store.upsert(
                    stock_id=idx["id"],
                    interval=interval.value,
                    earliest=candles[0].timestamp,
                    latest=candles[-1].timestamp,
                    candle_count=len(candles),
                    source="angel_one",
                )
                success += 1
                print(f"  [{i+1}/{len(indices)}] {idx['symbol']:25s} -> {len(candles)} candles")
            else:
                print(f"  [{i+1}/{len(indices)}] {idx['symbol']:25s} -> 0 candles")
            
            time.sleep(0.5)
        except Exception as e:
            print(f"  [{i+1}/{len(indices)}] {idx['symbol']:25s} -> ERROR: {e}")
    
    print(f"  >> {success} indices filled for {interval.value}")

print("\nDone!")
