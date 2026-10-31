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
