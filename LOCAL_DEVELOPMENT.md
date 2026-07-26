# Local development runtime

This development-only runtime starts the Stockwicks FastAPI portal and a
disposable PostgreSQL database. C3 adds direct, read-only daily PSX market data
from a host-side `psx-stock-watcher` SQLite database. Redis and one Celery worker
remain optional. It does not run Celery Beat, perform Schwab OAuth in PSX mode,
or enable paper/live brokerage actions.

## Prerequisites

- Docker Engine with Compose v2
- `openssl` (or another secure random generator)
- Python 3.12.3 only when running tests or regenerating locks on the host

## Configure and start

```bash
cp env.dev.example .env.dev
openssl rand -hex 32
```

Put independently generated values in `SECRET_KEY`, `MASTER_ADMIN_API_KEY`, and
`POSTGRES_PASSWORD` in `.env.dev`. Keep that file uncommitted. Use a simple
URL-safe database password because Compose constructs `DATABASE_URL` from it.

Set `PSX_DB_HOST_PATH` to the absolute host path of `psx_watcher.db`. A Linux
example has the shape
`/absolute/path/to/psx-stock-watcher/data/psx_watcher.db`. Docker Desktop and
WSL users must use a path visible to the WSL/Docker environment.

Compose mounts the file at the distinct container path
`/market-data/psx.db:ro`. The provider also opens it with SQLite `mode=ro` and
enables `PRAGMA query_only`, so the mount and driver independently enforce
read-only access.

Start the default services (`web` and `postgres` only):

```bash
docker compose --env-file .env.dev -f compose.dev.yml up --build
```

Check the runtime:

```bash
curl http://127.0.0.1:8101/healthz
python3 scripts/verify_dev_runtime.py
docker compose --env-file .env.dev -f compose.dev.yml logs web postgres
```

The web service runs the idempotent ORM bootstrap before Uvicorn. To verify it
twice explicitly:

```bash
docker compose --env-file .env.dev -f compose.dev.yml exec web python -m scripts.dev_bootstrap
docker compose --env-file .env.dev -f compose.dev.yml exec web python -m scripts.dev_bootstrap
```

## PSX provider and compatibility subset

`MARKET_DATA_PROVIDER=psx_sqlite` selects an in-process provider at the shared
stock price-history boundary. This is smaller than a separate HTTP service:
Stockwicks already consumes Python DataFrames and Schwab-shaped candle
dictionaries, so C3 needs no service, port, authentication layer, or network
hop. `MARKET_DATA_PROVIDER=schwab` preserves the legacy path.

Required C3 settings are:

```text
MARKET_DATA_PROVIDER=psx_sqlite
PSX_DB_PATH=/market-data/psx.db
PSX_PRICE_MODE=adjusted
```

Only `frequencyType=daily`, `frequency=1` (Stockwicks `1d`) with inclusive
`startDate`/`endDate` millisecond bounds is supported. Minute, weekly, monthly,
extended-hours, period-only, and previous-close requests fail explicitly. C3
does not claim general Schwab API compatibility.

Each candle contains `datetime`, `open`, `high`, `low`, `close`, and `volume`:

- OHLC maps to `open_adj`, `high_adj`, `low_adj`, and `close_adj`.
- Volume maps to the source `volume_adj`. PSX C8 defines this adjusted field to
  preserve traded-value continuity; C3 does not invent or recalculate it.
- Only authoritative `open_missing=1` rows use `open := close_adj`, with every
  fallback counted.
- Raw OHLCV, `adj_factor`, and raw `ldcp` remain separate audit fields.
- `datetime` is Unix milliseconds for midnight `Asia/Karachi` on `trade_date`,
  independent of the host timezone.

The provider preserves open/close-outside-range values. Open-outside-range is a
structured warning; close-outside-range is informational because an official
PSX closing auction can finish outside the continuous-session range. Duplicate
dates, missing/non-finite/non-positive adjusted OHLC, `high < low`, and
negative/non-finite adjusted volume fail the request. Counts and at most five
affected dates per class appear in the machine-readable `quality` object.

Unknown symbols raise a distinct error. A known symbol with no rows in valid
bounds returns an empty candle list. Relative/missing paths, invalid SQLite
files, missing schema columns, unsupported price modes, and unknown providers
fail clearly.

## PSX verification

Temporary-database tests never use the production PSX database:

```bash
python -m pytest -q -s \
  tests/test_c3_psx_market_data.py \
  tests/test_c3_compose_contract.py
```

The bounded smoke below exercises the compatibility path. Its default range
includes DGKC's preserved 2023-02-27 open-outside-range bar and prints SHA-256,
size, and nanosecond mtime fingerprints before and after:

```bash
python -m scripts.verify_psx_market_data \
  --db "$PSX_DB_HOST_PATH" \
  --start 2023-02-23 \
  --end 2023-03-03 \
  --symbols DGKC OGDC
```

The final line must be `fingerprint_unchanged True`. This direct write attempt
inside the container must fail with a read-only error:

```bash
docker compose --env-file .env.dev -f compose.dev.yml exec web python -c \
  'import sqlite3; c=sqlite3.connect("file:/market-data/psx.db?mode=ro", uri=True, isolation_level=None); c.execute("create table forbidden(x)")'
```

Restarting and stopping the stack do not modify the source database:

```bash
docker compose --env-file .env.dev -f compose.dev.yml restart web
docker compose --env-file .env.dev -f compose.dev.yml down
```

## Optional worker

The `worker` profile starts Redis and a Celery worker. It does not start Beat,
and the worker listens only to the `default` and `replay` queues:

```bash
docker compose --env-file .env.dev -f compose.dev.yml --profile worker up --build
docker compose --env-file .env.dev -f compose.dev.yml --profile worker down
```

## Tests and dependency locks

Install and run the tests in an isolated Python 3.12.3 environment:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev.lock
.venv/bin/python -m pytest tests/test_c2_reduced_runtime.py tests/test_c2_compose_contract.py
```

The input files pin direct packages. The compiled locks pin and hash the full
dependency graph:

```bash
.venv/bin/python -m piptools compile --generate-hashes --allow-unsafe --resolver=backtracking \
  --output-file requirements/runtime.lock requirements/runtime.in
.venv/bin/python -m piptools compile --generate-hashes --allow-unsafe --resolver=backtracking \
  --output-file requirements/dev.lock requirements/dev.in
```

## Stop or reset

Stop services while retaining disposable data:

```bash
docker compose --env-file .env.dev -f compose.dev.yml down
```

Reset only the named C2 PostgreSQL volume:

```bash
docker compose --env-file .env.dev -f compose.dev.yml down
docker volume rm stockwicks-c2-postgres-data
```

The bootstrap creates tables known to the current SQLAlchemy model registry,
with no users or production data. It is not a replacement for versioned
migrations. Feature-specific schema helpers are disabled in reduced mode, so
feature completeness outside health/login/static and C3 daily-history paths
remains unverified. C3 does not add intraday data, live quotes, strategies,
indicators, signals, fills, PSX calendars, ingestion, circuit logic, or trading.
