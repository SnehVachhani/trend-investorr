from pathlib import Path
import json,runpy,sys
sys.path.insert(0,str(Path.cwd()))
from datetime import datetime,timedelta
from freezegun import freeze_time
from core.constants import IST,CandleInterval
from data.data_ingestion import DataIngestionService
ns=runpy.run_path("tests/data/test_incremental_sync.py")
class Fetcher:
 def __init__(self):self.calls=[]
 def get_candles_range(self,**kw):self.calls.append(kw);return []
coverage=ns["_BackfillCoverage"]()
coverage.coverage={"earliest_ts":datetime(2026,9,1,9,15,tzinfo=IST),"latest_ts":datetime(2026,9,15,15,25,tzinfo=IST),"candle_count":74,"history_complete":False}
fetcher=Fetcher();service=ns["_make_backfill_service"](fetcher,coverage)
# The synthetic stored session has both endpoints but lacks an interior bar.
missing=datetime(2026,9,15,11,0,tzinfo=IST)
stored=[datetime(2026,9,15,9,15,tzinfo=IST)+timedelta(minutes=5*i) for i in range(75)]
stored.remove(missing)
service._candle_store.rows=[(1,ts,100,101,99,100,1000,100) for ts in stored]
with freeze_time("2026-09-15 13:00:00+00:00"):
 stats=service.backfill(days=1,interval=CandleInterval.FIVE_MINUTE,max_workers=1,max_stock_retries=0,aggregate_daily=False)
 current=service._is_interval_current(stored[-1],datetime(2026,9,15,18,30,tzinfo=IST),CandleInterval.FIVE_MINUTE)
setup=runpy.run_path("tests/strategies/test_fundamental_setup_backtester.py")
a=setup["run"](setup["sweep_candles"](),"consolidation_sweep_reclaim")
b=setup["run"](setup["sweep_candles"](),"consolidation_sweep_reclaim")
trades=[a["trades"][0],b["trades"][0]]
result={"ingestion":{"stored_bars":len(stored),"expected_regular_five_minute_bars":75,"interior_missing":missing.isoformat(),"latest_considered_current":current,"backfill_stats":stats,"provider_calls":len(fetcher.calls)},"independent_wallets":{"stocks":2,"wallet_per_stock":100000,"same_entry_timestamp":trades[0]["entry_date"]==trades[1]["entry_date"],"combined_entry_cost":sum(t["entry_price"]*t["quantity"] for t in trades),"note":"Combined gross notional excludes charges. Existing mode intentionally gives each stock its own wallet; this is a feature boundary, not a broken existing contract."}}
Path(".codex-attachments/next-parallel-research-20261030/probe-results.json").write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
