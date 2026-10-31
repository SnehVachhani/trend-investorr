import os,sys,subprocess,tempfile,zipfile,json,hashlib
from pathlib import Path
root=Path.cwd();evidence=root/'.codex-attachments/shared-starter-20261029'
meta=json.loads((evidence/'package.json').read_text())
base=Path(tempfile.mkdtemp(prefix='algoanalyst-backend-verify-'))
with zipfile.ZipFile(meta['archive']) as z:
 names=z.namelist()
 assert len(names)==meta['file_count']
 assert {Path(n).parts[0] for n in names}=={'AlgoAnalystBackend'}
 forbidden={'.git','.venv','venv','node_modules','__pycache__','.pytest_cache','.codex-attachments','frontend','spring-boot','graphify-out'}
 assert not any(set(Path(n).parts)&forbidden for n in names)
 assert not any(Path(n).name in {'AGENTS.md','CLAUDE.md','SKILL.md','.env','.vault'} for n in names)
 assert 'AlgoAnalystBackend/.gitignore' in names
 z.extractall(base)
work=base/'AlgoAnalystBackend'
for entry in meta['source_files']:
 assert hashlib.sha256((work/entry['path']).read_bytes()).hexdigest()==entry['source_sha256']
subprocess.run([sys.executable,'-m','venv',str(base/'venv')],check=True)
py=base/'venv/bin/python'
env=os.environ.copy();env.pop('PYTHONPATH',None)
with (evidence/'dependency-install.log').open('w') as log:
 r=subprocess.run([str(py),'-m','pip','install','-r','requirements.txt','-r','requirements-dev.txt'],cwd=work,stdout=log,stderr=subprocess.STDOUT,env=env)
 if r.returncode:raise SystemExit(f'Dependency install failed: {r.returncode}; see log')
checks={
'live-feed-baseline':['tests/data/test_live_candle_pipeline.py'],
'backtest-baseline':['tests/api/test_enhanced_momentum_replay_workers.py'],
'completed-trading-regression':['tests/trading/test_live_buy_broker_state.py','tests/trading/test_live_entry_reservation.py','tests/trading/test_live_square_off_coordination.py','tests/trading/test_risk_manager.py','tests/trading/test_live_sell_exposure.py','tests/trading/test_live_squareoff.py','tests/trading/test_circuit_breaker.py']}
results={}
for name,paths in checks.items():
 with (evidence/(name+'.log')).open('w') as log:
  r=subprocess.run([str(py),'-m','pytest','-q',*paths],cwd=work,stdout=log,stderr=subprocess.STDOUT,env=env)
 results[name]={'exit_code':r.returncode,'log':str(evidence/(name+'.log'))}
 print(name, r.returncode, flush=True)
(evidence/'verification.json').write_text(json.dumps({'extracted_workspace':str(work),'python':str(py),'checks':results},indent=2)+'\n')
print('Verification complete',flush=True)
