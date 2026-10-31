from pathlib import Path
import sys,json,tempfile
from datetime import datetime
import runpy
from types import SimpleNamespace
import pytest
from freezegun import freeze_time
from loguru import logger
logger.remove()
sys.path.insert(0,str(Path.cwd()))
from core.constants import IST,SignalType,AnalysisAction
from trading.order_manager import TradingMode
from tests.factories import make_signal,make_analysis_result
ns=runpy.run_path('tests/trading/test_live_buy_broker_state.py')
results=[]
with tempfile.TemporaryDirectory(prefix='focused-research-') as tmp, freeze_time(ns['SESSION_TIME_UTC']):
 base=Path(tmp)
 closed=ns['row'](token='11536',qty=0,symbol='TCS-EQ')
 closed.update(buyqty='100',sellqty='100',buyamount='500000',sellamount='250000',totalbuyvalue='500000',totalsellvalue='250000')
 for name,mode,broker_rows,paper_loss,side in [
  ('broker_closed_loss_paper_flat',TradingMode.LIVE,[closed],0,SignalType.BUY),
  ('paper_loss_broker_flat',TradingMode.LIVE,[], -250000,SignalType.BUY),
  ('dual_paper_loss_broker_flat',TradingMode.DUAL,[], -250000,SignalType.BUY),
  ('reducing_sell_after_loss',TradingMode.LIVE,[ns['row']()],-250000,SignalType.SELL),
 ]:
  folder=base/name;folder.mkdir()
  broker=ns['FakeBroker'](broker_rows);om,paper=ns['build'](folder,mode,broker)
  paper.get_daily_summary();paper._daily_realized_pnl=paper_loss
  om.update_prices({})
  result=om.execute_signal(make_signal(signal_type=side,price=ns['PRICE']),make_analysis_result(action=AnalysisAction.BUY if side is SignalType.BUY else AnalysisAction.SELL))
  results.append(dict(case=name,mode=mode.value,paper_realized=paper_loss,broker_calls=broker.position_calls,broker_orders=len(broker.placed),result_status=result.status.value if result else None,message=result.message if result else None,live_leg_status=result.live_leg.status.value if result and result.live_leg else None,live_leg_message=result.live_leg.message if result and result.live_leg else None))
# Boot real main with the accepted restart suite's existing fake services.
boot=runpy.run_path('tests/test_mid_session_restart.py')
with tempfile.TemporaryDirectory(prefix='postmarket-research-') as tmp, pytest.MonkeyPatch.context() as mp:
 fetches=[]
 mp.setattr(boot['FakeBhavcopyFetcher'],'fetch',lambda self:fetches.append('fetch') or [])
 h=boot['boot_main_at'](mp,Path(tmp),datetime(2026,3,4,16,30,tzinfo=IST))
 results.append(dict(case='restart_1630',postmarket_next_run=h.scheduler.next_run_time('post_market_analysis').isoformat(),postmarket_fired=[name for name,when in h.scheduler.fired if name=='post_market_analysis'],bhavcopy_calls_before_idle=len(fetches),ingestion_calls=list(h.ingestion.calls),live_started=h.ws.started,entries_result=h.try_open_long().message))
 # The fake fetch below is deliberately a required-input failure.
 def unavailable(self):raise RuntimeError('synthetic bhavcopy not yet available')
 mp.setattr(boot['FakeBhavcopyFetcher'],'fetch',unavailable)
 outcome=h.scheduler.run_sync('post_market_analysis')
 results.append(dict(case='postmarket_required_input_failure',scheduler_outcome=outcome,scheduler_errors=[name for name,error in h.scheduler.errors]))
print(json.dumps(results,indent=2))
Path(sys.argv[1]).write_text(json.dumps(results,indent=2))
