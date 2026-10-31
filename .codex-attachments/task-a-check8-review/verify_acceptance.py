from pathlib import Path
import tempfile,subprocess,os,json,shutil
p=Path(__file__).resolve().parent;m=json.loads((p/'workspace.json').read_text());w=Path(m['workspace']);base=Path(tempfile.mkdtemp(prefix='cb-final-pg-'));sock=base/'socket';sock.mkdir()
env=os.environ.copy();env.pop('PYTHONPATH',None);env.update(SCRATCH_PG_DIR=str(base/'db'),SCRATCH_PG_SOCKET_DIR=str(sock),SCRATCH_PG_PORT='55439')
started=False
try:
 start=subprocess.run(['sh','scripts/scratch_postgres.sh','start'],cwd=w,env=env,text=True,capture_output=True)
 (p/'scratch-start.log').write_text(start.stdout+start.stderr)
 start.check_returncode();started=True
 env.update(ALGOANALYST_TEST_PG_HOST=str(sock),ALGOANALYST_TEST_PG_PORT='55439',ALGOANALYST_TEST_PG_USER='postgres')
 paths=['tests/api/test_enhanced_momentum_replay_workers.py','tests/api/test_enhanced_momentum_backtest_job_durability.py','tests/api/test_enhanced_momentum_backtest_job_postgres.py']
 with (p/'acceptance-tests.log').open('w') as log:r=subprocess.run([m['python'],'-m','pytest','-q',*paths],cwd=w,env=env,stdout=log,stderr=subprocess.STDOUT)
 print('exit',r.returncode);print('\n'.join((p/'acceptance-tests.log').read_text().splitlines()[-12:]))
finally:
 if started:
  stop=subprocess.run(['sh','scripts/scratch_postgres.sh','stop'],cwd=w,env=env,text=True,capture_output=True);(p/'scratch-stop.log').write_text(stop.stdout+stop.stderr);stop.check_returncode()
 shutil.rmtree(base)
