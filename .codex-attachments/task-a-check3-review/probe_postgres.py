from pathlib import Path
import os,sys,json,tempfile,subprocess,getpass,importlib.util,threading
from datetime import datetime,timedelta,timezone,date
from concurrent.futures import ThreadPoolExecutor
from loguru import logger
logger.remove()
e=Path(__file__).resolve().parent;meta=json.loads((e/'workspace.json').read_text());w=Path(meta['workspace']);sys.path.insert(0,str(w))
import psycopg2
from storage.database import Database,SCHEMA_MIGRATIONS
from tests.api.test_enhanced_momentum_backtest_job_durability import _NeverStartedThread
base=Path(tempfile.mkdtemp(prefix='cbpg-'));data=base/'db';sock=base/'socket';sock.mkdir();log=base/'pg.log';db=None;started=False;results={}
try:
 subprocess.run(['/usr/local/bin/initdb','-D',str(data),'-A','trust','--no-locale'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 subprocess.run(['/usr/local/bin/pg_ctl','-D',str(data),'-l',str(log),'-o',f"-F -c listen_addresses='' -k {sock} -p 55439",'-w','start'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);started=True
 db=Database(host=str(sock),port=55439,dbname='postgres',user=getpass.getuser());db.connect()
 selected=[s for s in SCHEMA_MIGRATIONS if any(x in s for x in ['CREATE TABLE IF NOT EXISTS enhanced_momentum_backtest_runs','ALTER TABLE enhanced_momentum_backtest_runs','CREATE TABLE IF NOT EXISTS enhanced_momentum_backtest_jobs','CREATE UNIQUE INDEX IF NOT EXISTS idx_enhanced_momentum_backtest_jobs_single_active'])]
 for _ in range(2):
  for sql in selected:db.execute(sql)
 results['migration_statements']=len(selected);results['migrations_applied_twice']=True
 barrier=threading.Barrier(2)
 class Proxy:
  def __getattr__(self,n):return getattr(db,n)
  def execute_returning(self,sql,params=()):
   if 'INSERT INTO enhanced_momentum_backtest_jobs' in sql:barrier.wait(timeout=5)
   return db.execute_returning(sql,params)
 def load(name):
  spec=importlib.util.spec_from_file_location(name,w/'api/server.py');m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
  m.get_db=lambda:Proxy();m.Thread=_NeverStartedThread;m.build_filter_presets=lambda:[]
  return m
 a=load('pg_worker_a');b=load('pg_worker_b')
 def start(m):
  try:return m.start_enhanced_momentum_backtest(m.EnhancedMomentumBacktestRequest())['status']
  except m.HTTPException as ex:return ex.status_code
 with ThreadPoolExecutor(max_workers=2) as pool:results['concurrent_admission']=list(pool.map(start,[a,b]))
 results['job_count']=db.fetch_one('SELECT count(*) AS n FROM enhanced_momentum_backtest_jobs')['n']
 # Force actual unique-index violation through the app wrapper, so exception translation is tested too.
 winner=db.fetch_one('SELECT job_id,worker_id FROM enhanced_momentum_backtest_jobs');owner=a if a._ENHANCED_BACKTEST_WORKER_ID==winner['worker_id'] else b;reader=b if owner is a else a
 real=db.execute_returning
 def duplicate_with_stale_snapshot(sql,params=()):
  if 'INSERT INTO enhanced_momentum_backtest_jobs' in sql:sql=sql.replace("WHERE NOT EXISTS (\n                SELECT 1 FROM enhanced_momentum_backtest_jobs WHERE status IN ('queued', 'running')\n            )",'')
  return real(sql,params)
 owner.get_db=lambda:db
 job={'job_id':'f'*32,'status':'queued','created_at':datetime.now(timezone.utc),'started_at':None,'completed_at':None,'progress':{},'run_id':None,'error':None}
 db.execute_returning=duplicate_with_stale_snapshot
 try:results['unique_violation_maps_to_not_admitted']=owner._admit_enhanced_backtest_job(job,owner.EnhancedMomentumBacktestRequest()) is False
 finally:db.execute_returning=real
 reader.get_db=lambda:db
 jid=winner['job_id']
 db.execute("UPDATE enhanced_momentum_backtest_jobs SET status='running',heartbeat_at=NOW()-INTERVAL '120 seconds' WHERE job_id=%s",(jid,))
 results['stale_settlement']=reader.get_enhanced_momentum_backtest_job(jid)['status']
 results['late_status_write']=owner._persist_enhanced_backtest_job_changes(jid,{'status':'completed'})
 values=(date(2026,6,29),date(2026,6,30),1,'{}','{}','{}','{}','[]','[]')
 try:owner._save_enhanced_backtest_run(db,values,job_id=jid);results['late_result_save']='accepted'
 except owner._EnhancedBacktestOwnershipLost:results['late_result_save']='rejected'
 results['run_count']=db.fetch_one('SELECT count(*) AS n FROM enhanced_momentum_backtest_runs')['n']
 # Current owner can save and complete via the actual SQL path.
 newjid='a'*32;job.update(job_id=newjid)
 assert owner._admit_enhanced_backtest_job(job,owner.EnhancedMomentumBacktestRequest())
 assert owner._persist_enhanced_backtest_job_changes(newjid,{'status':'running'})=='written'
 run=owner._save_enhanced_backtest_run(db,values,job_id=newjid)
 results['valid_result_saved']=run is not None
 results['valid_completion']=owner._persist_enhanced_backtest_job_changes(newjid,{'status':'completed','run_id':run['id']})
 # A repeated save before final completion is not unique by job ID.
 # Demonstrate directly on a running job using the same production save function.
 otherjid='b'*32;job.update(job_id=otherjid)
 assert owner._admit_enhanced_backtest_job(job,owner.EnhancedMomentumBacktestRequest())
 owner._persist_enhanced_backtest_job_changes(otherjid,{'status':'running'})
 first=owner._save_enhanced_backtest_run(db,values,job_id=otherjid)
 second=owner._save_enhanced_backtest_run(db,values,job_id=otherjid)
 results['repeat_save_before_completion']={'first_run':first['id'],'second_run':second['id']}
finally:
 if db:db.disconnect()
 if started:subprocess.run(['/usr/local/bin/pg_ctl','-D',str(data),'-m','fast','-w','stop'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
 import shutil;shutil.rmtree(base)
print(json.dumps(results,indent=2));(e/'postgres-results.json').write_text(json.dumps(results,indent=2)+'\n')
