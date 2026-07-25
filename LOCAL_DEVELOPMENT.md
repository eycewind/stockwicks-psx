# C2 local development runtime

This development-only runtime starts the Stockwicks FastAPI portal and a
disposable PostgreSQL database. Redis and one Celery worker are optional. It
does not provide PSX data, run Celery Beat, perform Schwab OAuth, or enable
paper/live brokerage actions.

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
feature completeness outside health/login/static smoke paths remains
unverified. C3 and later contracts must supply PSX data integration.
