from pathlib import Path
import json,tempfile,subprocess,os,getpass,shutil
p=Path(__file__).resolve().parent;m=json.loads((p/'workspace.json').read_text());base=Path(tempfile.mkdtemp(prefix='cbpg-'));data=base/'db';sock=base/'socket';sock.mkdir();started=False
try:
 subprocess.run(['/usr/local/bin/initdb','-D',str(data),'-A','trust','--no-locale','--encoding=UTF8'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 subprocess.run(['/usr/local/bin/pg_ctl','-D',str(data),'-l',str(base/'pg.log'),'-o',f"-F -c listen_addresses='' -k {sock} -p 55439",'-w','start'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);started=True
 env=os.environ.copy();env.pop('PYTHONPATH',None);env.update(ALGOANALYST_TEST_PG_HOST=str(sock),ALGOANALYST_TEST_PG_PORT='55439',ALGOANALYST_TEST_PG_USER=getpass.getuser())
 with (p/'postgres-tests.log').open('w') as log:r=subprocess.run([m['python'],'-m','pytest','-q','tests/api/test_enhanced_momentum_backtest_job_postgres.py'],cwd=m['workspace'],env=env,stdout=log,stderr=subprocess.STDOUT)
 print('exit',r.returncode);print('\n'.join((p/'postgres-tests.log').read_text().splitlines()[-10:]))
finally:
 if started:subprocess.run(['/usr/local/bin/pg_ctl','-D',str(data),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 shutil.rmtree(base)
