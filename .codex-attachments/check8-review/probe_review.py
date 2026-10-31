from pathlib import Path
import sys,json,tempfile
review=Path(__file__).resolve().parent
work=Path((review/'workspace.txt').read_text())
sys.path.insert(0,str(work))
from freezegun import freeze_time
from loguru import logger
from tests.trading.test_live_entry_reservation import FakeBroker,build,buy
from tests.factories import make_signal,make_analysis_result
from trading.risk_manager import RiskConfig
from core.constants import AnalysisAction,SignalType
import trading.order_manager
assert Path(trading.order_manager.__file__).resolve()==(work/'trading/order_manager.py').resolve()
logger.remove()
results=[]
with tempfile.TemporaryDirectory(prefix='check5-probes-') as tmp:
 root=Path(tmp)
 with freeze_time('2026-03-04 04:30:00'):
  for label,response in [('empty-dict',{}),('missing-status',{'message':'upstream response incomplete'})]:
   class MalformedBroker(FakeBroker):
    def placeOrder(self,params):
     self._accept(params)
     return response
   b=MalformedBroker(hidden={'BRK1','BRK2'});m=build(root/label,b)
   r1=buy(m);r2=buy(m)
   results.append({'case':'accepted but reply '+label,'statuses':[r1.status.value,r2.status.value],'submissions':len(b.placed)})
  b=FakeBroker();m=build(root/'filled-lag',b)
  r1=buy(m)
  # The order service sees the fill before the separate position service reflects it.
  b.orders[0].update(status='complete',orderstatus='complete',filledshares=b.orders[0]['quantity'],unfilledshares='0',averageprice='2830')
  r2=buy(m)
  results.append({'case':'order complete but position update delayed','statuses':[r1.status.value,r2.status.value],'submissions':len(b.placed),'position_rows':len(b.rows)})
  b=FakeBroker(hidden={'BRK1','BRK2'});m=build(root/'cross-symbol',b)
  m._risk_manager.update_config(RiskConfig(total_capital=100000,max_capital_deployed_pct=60,max_capital_per_trade_pct=50))
  rs=[]
  for symbol,token in [('AAA','1'),('BBB','2')]:
   r=m.execute_signal(make_signal(symbol=symbol,token=token,price=1000,signal_type=SignalType.BUY),make_analysis_result(action=AnalysisAction.BUY));rs.append(r.status.value)
  results.append({'case':'sequential hidden entries in different instruments','statuses':rs,'deployment_cap':60000,'submitted_notional':sum(int(p['quantity'])*1000 for p in b.placed),'submissions':len(b.placed)})
  b=FakeBroker(reply='lost',hidden={'BRK1','BRK2'});m=build(root/'next-day',b);r1=buy(m)
 with freeze_time('2026-03-05 04:30:00'):
  m=build(root/'next-day',b);r2=buy(m)
  results.append({'case':'unknown prior-day submission, no broker resolution','statuses':[r1.status.value,r2.status.value],'submissions':len(b.placed)})
print(json.dumps(results,indent=2))
(review/'probe-results.json').write_text(json.dumps(results,indent=2)+'\n')
