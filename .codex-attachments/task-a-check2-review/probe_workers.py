from pathlib import Path
import sys,json,importlib.util,threading
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
from loguru import logger
logger.remove()
e=Path(__file__).resolve().parent;meta=json.loads((e/'workspace.json').read_text());w=Path(meta['workspace']);sys.path.insert(0,str(w))
import pytest
from tests.api.test_enhanced_momentum_backtest_job_durability import _FakeRunStore,_NeverStartedThread

def worker(name,db):
 spec=importlib.util.spec_from_file_location(name,w/'api/server.py');m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
 m.get_db=lambda:db;m.Thread=_NeverStartedThread;m.build_filter_presets=lambda:[]
 return m

barrier=threading.Barrier(2)
class RacingStore(_FakeRunStore):
 def fetch_all(self,sql,params=()):
  snapshot=super().fetch_all(sql,params)
  if 'FROM enhanced_momentum_backtest_jobs' in sql:barrier.wait(timeout=5)
  return snapshot
s=RacingStore();a=worker('review_api_worker_a',s);b=worker('review_api_worker_b',s)
def start(m):
 try:return m.start_enhanced_momentum_backtest(m.EnhancedMomentumBacktestRequest())['status']
 except Exception as ex:return str(ex)
with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(start,[a,b]))
results=[{'case':'two independent API module instances read empty admission state together','statuses':statuses,'persisted_jobs':len(s.jobs)}]

s2=_FakeRunStore();a.get_db=lambda:s2;b.get_db=lambda:s2;a._enhanced_momentum_backtest_jobs={};b._enhanced_momentum_backtest_jobs={}
req=a.EnhancedMomentumBacktestRequest()
job=a.start_enhanced_momentum_backtest(req)['job_id']
a._update_enhanced_backtest_job(job,status='running')
s2.jobs[job]['heartbeat_at']=datetime.now(timezone.utc)-timedelta(seconds=120)
before=b.get_enhanced_momentum_backtest_job(job)
# A late worker uses the actual update function after B durably settled its job.
a._update_enhanced_backtest_job(job,status='completed',run_id=4242,error=None)
after=b.get_enhanced_momentum_backtest_job(job)
results.append({'case':'expired owner publishes after another API instance settles interruption','before':before['status'],'after':after['status'],'old_owner_write_accepted':s2.jobs[job]['status']=='completed'})
print(json.dumps(results,indent=2));(e/'probe-results.json').write_text(json.dumps(results,indent=2)+'\n')
