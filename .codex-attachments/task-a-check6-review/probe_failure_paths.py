from pathlib import Path
import sys,json
p=Path(__file__).resolve().parent;m=json.loads((p/'workspace.json').read_text());sys.path.insert(0,m['workspace'])
from loguru import logger
logger.remove()
import pytest
from api import server
from core.exceptions import DatabaseError
from tests.api.test_enhanced_momentum_backtest_job_durability import _FakeRunStore,_stub_replay_work,_ApiProcess,_InlineThread,_request,_poll,_NeverStartedThread
out=[]
with pytest.MonkeyPatch.context() as mp:
 db=_FakeRunStore();calls=_stub_replay_work(mp,db);_ApiProcess('a',_InlineThread).activate(mp,db)
 original=db.execute_returning;count=[0];db.lose_next_run_save_response=True
 def save_failure(sql,params):
  if 'INSERT INTO enhanced_momentum_backtest_runs' in sql:
   count[0]+=1
   if count[0]==2:raise DatabaseError('retry transport unavailable; response not obtained')
  return original(sql,params)
 mp.setattr(db,'execute_returning',save_failure)
 first=server.start_enhanced_momentum_backtest(_request(submission_id='two-save-failures'))
 before=_poll(first['job_id']);_ApiProcess('b',_NeverStartedThread).activate(mp,db)
 retried=server.start_enhanced_momentum_backtest(_request(submission_id='two-save-failures'))
 out.append({'case':'saved result committed, first reply lost, retry transport fails, job-status writes recover','saved_runs':len(db.runs),'status':before['status'],'returned_run_id':retried.get('run_id'),'retry_status':retried['status'],'replay_calls':len(calls)})
with pytest.MonkeyPatch.context() as mp:
 db=_FakeRunStore();_stub_replay_work(mp,db)
 class CannotStart:
  def __init__(self,*a,**kw):pass
  def start(self):raise RuntimeError('synthetic thread start failure')
 _ApiProcess('a',CannotStart).activate(mp,db)
 try:server.start_enhanced_momentum_backtest(_request(submission_id='launch-failed'))
 except RuntimeError:pass
 retry=server.start_enhanced_momentum_backtest(_request(submission_id='launch-failed'))
 mp.setattr(server,'_launch_enhanced_backtest_worker',lambda *a: (_ for _ in ()).throw(AssertionError('should launch on recovery')))
 from datetime import datetime,timedelta,timezone
 for row in db.jobs.values():row['heartbeat_at']=datetime.now(timezone.utc)-timedelta(hours=1)
 polled=server.get_enhanced_momentum_backtest_job(retry['job_id'])
 out.append({'case':'worker thread never starts, same-process retry and later poll','retry_status':retry['status'],'later_poll_status':polled['status'],'durable_status':db.jobs[retry['job_id']]['status']})
print(json.dumps(out,indent=2));(p/'failure-path-results.json').write_text(json.dumps(out,indent=2)+'\n')
