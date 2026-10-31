# AlgoAnalyst Backend

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

Replay job durability (fake storage, per-process state, no database):

```sh
/tmp/venv/bin/python -m pytest -q tests/api/test_enhanced_momentum_backtest_job_durability.py
```

Replay job coordination on a real PostgreSQL with genuinely separate API
processes (migration upgrade, admission/fence/save statements, simultaneous
admissions, concurrent retries, an expired worker resuming after replacement):

```sh
eval "$(scripts/scratch_postgres.sh start)"   # throwaway cluster on a unix socket
/tmp/venv/bin/python -m pytest -q tests/api/test_enhanced_momentum_backtest_job_postgres.py
scripts/scratch_postgres.sh stop
```

The PostgreSQL-backed file needs `initdb`/`pg_ctl` on PATH (or point
`ALGOANALYST_TEST_PG_HOST`/`_PORT`/`_USER`/`_PASSWORD` at any disposable
server). It creates and drops its own databases, uses `SIGSTOP`/`SIGCONT` to
freeze a worker process (POSIX only), and **skips** when the variables are unset.

Focused acceptance selection for the replay job work:

```sh
eval "$(scripts/scratch_postgres.sh start)"
/tmp/venv/bin/python -m pytest tests/api/test_enhanced_momentum_replay_workers.py \
    tests/api/test_enhanced_momentum_backtest_job_durability.py \
    tests/api/test_enhanced_momentum_backtest_job_postgres.py
scripts/scratch_postgres.sh stop
```

Existing tests use synthetic fixtures and fake services. They do not need live
broker credentials or production data. Running the actual application requires
separately configured external services, including PostgreSQL.

## Included and excluded

The source includes the completed live-entry and square-off safeguards and
durable Enhanced Momentum replay job tracking (`enhanced_momentum_backtest_jobs`,
covered by `tests/api/test_enhanced_momentum_backtest_job_durability.py`). No
live-feed lifecycle fixes have been applied in this snapshot.
API runtime and test-client dependencies are included in the requirements files.

Git history, agent instruction files, virtual environments, local configuration,
vaults, runtime data, generated logs and caches are excluded. Credential-like
values in security tests are synthetic fixtures; tests create their own temporary
vaults and keys.
