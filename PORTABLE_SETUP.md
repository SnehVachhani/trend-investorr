# Trend-Investorr portable code bundle

This folder contains the project source code and the files needed to install its dependencies on another laptop. Installed dependencies, generated build output, local caches, runtime logs, exports, database data, and credentials are intentionally not included.

## Included

- Python application code, tests, scripts, and configuration
- `requirements.txt` and `requirements-dev.txt`
- Next.js frontend source and npm manifests (`frontend/package.json` and `frontend/package-lock.json`)
- Spring Boot source, database migrations, and `spring-boot/pom.xml`
- `.env.example` as a safe configuration template
- The required stock/company reference directory at `data_cache/company_directory.json`
- Project-level test/configuration files

## Excluded for portability and safety

- Python virtual environments and caches
- `frontend/node_modules` and `.next` build output
- `spring-boot/target` compiled Maven output
- Generated market-data caches, logs, CSV exports, and test caches; the required company directory is retained
- `.env`, `.vault`, and other local credentials

## Setup on the new Windows laptop

Install these first:

- Python 3.11 or newer
- Node.js with npm
- JDK 17
- Maven 3.9 or newer
- PostgreSQL if you want to run the database-backed services

From this folder:

```powershell
# Python environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt

# Local configuration; fill in real values locally and never commit this file
Copy-Item .env.example .env

# Frontend dependencies
Set-Location frontend
npm install
npm run dev
```

Run the Spring Boot service from another terminal at the bundle root:

```powershell
Set-Location spring-boot
mvn spring-boot:run
```

The Spring Boot service expects PostgreSQL and reads database/API settings from environment variables. Create the `trend_investorr` database, then configure the required values in the local `.env`/environment before starting services.

## Verification commands

```powershell
# From the bundle root
python -m pytest

Set-Location frontend
npm run lint
npm run build

Set-Location ..\spring-boot
mvn test
```

`npm install`, Python package installation, and Maven dependency downloads are intentionally performed on the destination laptop rather than stored in this bundle.
