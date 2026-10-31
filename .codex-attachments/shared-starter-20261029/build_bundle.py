from pathlib import Path
import shutil,json,hashlib,zipfile,re
root=Path.cwd()
out=Path('/Users/macbookpro/Documents/Codex/AlgoAnalystBackend')
archive=Path('/Users/macbookpro/Downloads/AlgoAnalystBackend.zip')
assert not out.exists(), 'Refusing to replace an existing workspace'
assert not archive.exists(), 'Refusing to replace an existing archive'
out.mkdir(parents=True)
dirs=['analyzers','api','cli','config','core','data','engine','notifications','scheduler','scripts','storage','strategies','tests','trading']
manifest=[]
for name in dirs:
 for source in sorted((root/name).rglob('*.py')):
  if any(p in {'__pycache__','.venv','node_modules'} for p in source.parts):continue
  rel=source.relative_to(root);dest=out/rel;dest.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(source,dest)
  manifest.append({'path':str(rel),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
for rel in ['main.py','pytest.ini','config/sectors.json','requirements.txt','requirements-dev.txt']:
 dest=out/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(root/rel,dest)
requirements=(out/'requirements.txt').read_text()
requirements=requirements.replace('# Scheduling\n','# REST API\nfastapi>=0.115.0\nuvicorn>=0.30.0\n\n# Scheduling\n')
(out/'requirements.txt').write_text(requirements)
with (out/'requirements-dev.txt').open('a') as f:f.write('\n# API test client\nhttpx>=0.27.0\n')
shutil.copy2('/Users/macbookpro/Documents/Codex/AlgoAnalyst/.gitignore',out/'.gitignore')
with (out/'.gitignore').open('a') as f:f.write('\n# Private runtime and local tooling\n.codex-attachments/\ngraphify-out/\nreports/\nentry_reservations/\nsquare_off_intents/\n')
(out/'README.md').write_text('''# AlgoAnalyst Backend

Python backend for market data, stock scanners, historical backtests and trading.
Source modules and tests are included. The web frontend and Java service are not
part of this backend workspace.

## Setup

Use Python 3.12. Run each command separately from this folder:

```sh
python3 -m venv /tmp/venv
/tmp/venv/bin/python -m pip install -r requirements.txt
/tmp/venv/bin/python -m pip install -r requirements-dev.txt
```

## Focused existing checks

Live-feed parsing and candle behavior:

```sh
/tmp/venv/bin/python -m pytest -q tests/data/test_live_candle_pipeline.py
```

Enhanced Momentum replay behavior:

```sh
/tmp/venv/bin/python -m pytest -q tests/api/test_enhanced_momentum_replay_workers.py
```

The replay file currently has two known baseline failures: a test patches the
removed `_enhanced_backtest_profile_stock_batch` helper, and another expects UTC
boundaries where current queries use Asia/Kolkata. Inspect the intended behavior
before updating either check. These are not passing baseline checks.

Existing tests use synthetic fixtures and fake services. They do not need live
broker credentials or production data. Running the actual application requires
separately configured external services, including PostgreSQL.

## Included and excluded

The source includes the completed live-entry and square-off safeguards. No new
backtest-job or live-feed lifecycle fixes have been applied in this snapshot.
API runtime and test-client dependencies are included in the requirements files.

Git history, agent instruction files, virtual environments, local configuration,
vaults, runtime data, generated logs and caches are excluded. Credential-like
values in security tests are synthetic fixtures; tests create their own temporary
vaults and keys.
''')
patterns={
'private_key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
'github_token':r'\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}',
'google_key':r'\bAIza[A-Za-z0-9_-]{30,}',
'openai_key':r'\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}',
'aws_key':r'\bAKIA[0-9A-Z]{16}\b',
'jwt':r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}',
'personal_path':r'/Users/[^/\s]+/',
'email':r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
}
findings=[]
for f in sorted(out.rglob('*')):
 if not f.is_file():continue
 body=f.read_text()
 for category,pattern in patterns.items():
  for match in re.finditer(pattern,body):
   findings.append({'path':str(f.relative_to(out)),'line':body.count('\n',0,match.start())+1,'category':category})
for entry in manifest:
 assert hashlib.sha256((out/entry['path']).read_bytes()).hexdigest()==entry['source_sha256']
files=[f for f in sorted(out.rglob('*')) if f.is_file()]
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
 for f in files:z.write(f,Path(out.name)/f.relative_to(out))
metadata={'folder':str(out),'archive':str(archive),'file_count':len(files),'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'packaging_only_changes':['README.md','.gitignore','requirements.txt adds fastapi and uvicorn','requirements-dev.txt adds httpx'],'source_files':manifest,'scan_findings':findings,'archive_files':[str(f.relative_to(out)) for f in files]}
(root/'.codex-attachments/shared-starter-20261029/package.json').write_text(json.dumps(metadata,indent=2)+'\n')
print(json.dumps({k:metadata[k] for k in ['archive','file_count','bytes','scan_findings']},indent=2))
