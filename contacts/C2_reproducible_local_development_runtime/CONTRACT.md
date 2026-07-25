# Contract C2: Reproducible Local Development Runtime

## 1. Objective

Create a reproducible, development-only runtime for the inherited Stockwicks
FastAPI application.

The runtime must support:

- starting the web application locally;
- connecting it to a disposable PostgreSQL database;
- rendering the portal and serving its static assets;
- running health and import/startup smoke tests;
- optionally starting Redis and a Celery worker through an explicit profile;
- later addition of the PSX compatibility service without redesigning the
  runtime.

The runtime must keep all live trading, Schwab OAuth, external brokerage calls,
Celery Beat schedules, and real-order functionality disabled.

This contract establishes the runtime only. It does **not** implement PSX market
data, integrate an algorithm, or evaluate strategy performance.

## 2. Background and Constraints

C1 established that the repository contains:

- a FastAPI/Uvicorn web application;
- PostgreSQL-backed SQLAlchemy models;
- Redis/Celery background processing;
- Jinja2 templates and static assets;
- local `data/`, `logs/`, and `models/` persistence;
- no verified dependency manifest or lock file;
- no application Dockerfile or Compose definition;
- no versioned PostgreSQL migration history;
- startup paths that may create database tables as side effects;
- a dirty inherited worktree containing unrelated changes.

The executioner must inspect the current repository rather than assuming that C1
captured every dependency or startup requirement.

## 3. In Scope

### T1 — Repository and runtime preflight

Before changing files:

1. Read the C1 contract/delivery and any repository instructions.
2. Record:
   - current branch and commit;
   - concise `git status`;
   - Python version evidence;
   - application entry point;
   - current configuration loader;
   - database initialization behavior;
   - whether any dependency metadata has appeared since C1.
3. Identify pre-existing modified, deleted, and untracked files.
4. Do not restore, delete, stage, reformat, or overwrite unrelated user changes.
5. If required target files already contain uncommitted user edits, stop and
   report the overlap before modifying them.

### T2 — Pinned Python dependency definition

Create a source-controlled dependency definition suitable for building the
application image reproducibly.

Requirements:

- Pin every direct runtime dependency needed by the selected reduced runtime.
- Pin transitive dependencies through a generated lock file or fully hashed
  requirements lock where practical.
- Separate optional development/test dependencies from production runtime
  dependencies.
- Document the supported Python version and use the same version in the
  container image and automated checks.
- Infer dependencies from imports, deployment scripts, and actual startup/test
  failures. Do not claim that a static import list is complete.
- Resolve incompatible or abandoned packages explicitly; do not use unbounded
  versions such as `package>=x` in the reproducible lock.
- Do not include credentials, tokens, local absolute paths, virtual
  environments, or generated package caches.

The executioner may choose `pyproject.toml` plus a lock file, or a clear
`requirements.in`/compiled `requirements.txt` arrangement. The delivery must
explain the choice and give the exact regeneration command.

### T3 — Development container image

Create a development Dockerfile for the FastAPI application.

Requirements:

- Use the pinned Python version.
- Install dependencies from the locked definition.
- Set a non-root runtime user unless the repository has a demonstrated blocker.
- Copy only required application sources and assets.
- Create or mount writable `data/`, `logs/`, and `models/` paths.
- Expose the application port used by Compose.
- Start Uvicorn with the confirmed application import path.
- Include a useful container health check against `/healthz`.
- Avoid embedding secrets or copying `.env`, token files, database files,
  caches, logs, model outputs, or local virtual environments into the image.
- Add or update `.dockerignore` narrowly. Preserve existing ignore rules.

The image must not run schema-destructive setup, production deployment scripts,
nginx, systemd, Celery Beat, OAuth refresh, or live trading on startup.

### T4 — Development Compose topology

Create a development Compose definition containing:

#### Default services

- `web`
- `postgres`

#### Optional profile

- `redis`
- `worker`

Celery Beat must not be present or must be behind a separate disabled profile
that is not exercised in C2. Prefer omitting it.

Requirements:

- `web` must wait for a healthy PostgreSQL service.
- `worker`, when enabled, must wait for healthy PostgreSQL and Redis services.
- Use named volumes for disposable PostgreSQL data and appropriate application
  runtime paths.
- Bind the web service to localhost by default.
- Do not publish PostgreSQL or Redis ports unless required for a documented
  developer workflow; if published, bind them to localhost.
- Reserve a documented internal hostname/configuration seam for a future
  `psx-api`, but do not implement or add that service in C2.
- Use environment substitution and a sanitized example file.
- Do not mount the real PSX SQLite database in C2.
- Do not reference the user-specific path
  `/home/hassan/psx-stock-watcher/data/psx_watcher.db`.
- Add restart policies only where they do not hide startup failures during
  development.

The standard startup command must start only `web` and `postgres`. Redis and the
worker must require an explicit profile or explicit service selection.

### T5 — Sanitized local configuration and safety gates

Create a committed environment template, for example `.env.example`, containing
names and safe development values only.

It must include or document:

- application environment and port;
- generated-placeholder application/JWT secrets;
- PostgreSQL connection settings;
- optional Redis/Celery settings;
- `DATA_DIR`, `LOG_DIR`, and `MODEL_DIR`;
- trading safety flags;
- future market-data provider/base URL placeholders if the current settings
  model can safely accept them without implementing C3/C4 behavior.

Mandatory effective safety state:

- `TRADING_ENABLED=false`
- `LIVE_TRADING_ENABLED=false`
- `EMERGENCY_STOP=true`
- no real Schwab client ID, client secret, refresh token, or user token;
- no production database URL;
- no external order endpoint.

`PAPER_TRADING_ENABLED` may be `true` only if it cannot enable scheduled or live
broker actions in the reduced runtime. Otherwise default it to `false` and
explain how it will be enabled safely in a later contract.

If the application currently has insecure fallback secrets or connection
strings, C2 may remove or reject those fallbacks only where necessary for safe
startup. Such changes require focused tests and must be listed explicitly.

### T6 — Disposable PostgreSQL bootstrap

Provide a deterministic way to initialize enough schema for the reduced web
runtime and smoke tests.

Requirements:

- First inspect existing ORM metadata, initialization functions, SQL scripts,
  and startup side effects.
- Prefer an idempotent development bootstrap based on the application's
  authoritative models.
- Do not clone or connect to a production/client database.
- Do not copy production user data.
- Do not introduce Alembic or design a full migration history unless startup
  cannot otherwise be made deterministic; if that larger change is necessary,
  stop and propose a separate contract.
- The bootstrap must be safe to run twice.
- If seed data is required, use minimal synthetic development data and document
  it.
- Do not create a default privileged account with a fixed password.

The delivery must clearly distinguish:

- schema created for C2 smoke testing;
- tables/features not yet verified;
- remaining migration debt.

### T7 — Reduced-mode feature isolation

Make only the smallest changes required to start the web application without
external providers or background schedulers.

Requirements:

- No outbound Schwab, broker, OAuth, email, market-data, or model-provider
  request may be required for startup or `/healthz`.
- No Celery Beat task may start.
- No worker is started in the default profile.
- Unsupported routes may remain registered, but they must not make external
  calls merely because the application imports or starts.
- Preserve production behavior when existing production configuration is used,
  unless an existing unsafe default must be corrected.
- Any new reduced-mode/feature flag must have a narrow name, documented default,
  and test coverage.

Do not broadly delete or stub application modules to make startup pass.

### T8 — Smoke tests and verification

Add automated tests or scripts that verify the runtime without real external
services.

Minimum checks:

1. Locked dependencies install successfully in a clean image build.
2. Compose configuration validates.
3. `web` and `postgres` start from a clean disposable state.
4. PostgreSQL becomes healthy.
5. `/healthz` returns HTTP 200 and the expected body.
6. One representative public/login portal page returns a successful response
   and its HTML is rendered.
7. One static asset returns HTTP 200.
8. The schema/bootstrap succeeds from an empty database.
9. The schema/bootstrap is idempotent.
10. Default startup does not start Redis, worker, or Beat.
11. Optional Redis/worker profile starts only when explicitly requested.
12. Startup and smoke tests use no real Schwab credentials.
13. A network guard, request mock, log assertion, or equivalent evidence proves
    that the tested startup path does not contact Schwab or a broker host.
14. Trading safety flags inside the running web container have the required
    effective values.

Tests must use disposable data. They must not connect to the existing PSX SQLite
database or any production PostgreSQL instance.

If the current environment cannot run Docker, the executioner must still
validate static configuration and unit tests, but must label container startup
criteria unverified. Lack of Docker is not permission to claim completion.

### T9 — Developer documentation

Add concise local-development instructions covering:

- prerequisites;
- copying the environment template without committing secrets;
- generating development secrets;
- building and starting the default runtime;
- checking health and logs;
- starting/stopping the optional worker profile;
- running tests;
- resetting only the disposable development database;
- rebuilding/regenerating the dependency lock;
- known limitations;
- clear warning that PSX data integration is not part of C2.

Any reset command must target the named development volume precisely. Do not
publish a broad destructive command.

## 4. Explicitly Out of Scope

C2 must not:

- implement `PsxSqliteMarketDataProvider`;
- implement the PSX Schwab-compatible API;
- mount, copy, modify, or migrate `psx_watcher.db`;
- change PSX OHLC semantics or repair market data;
- route application history calls away from Schwab;
- alter strategy, indicator, signal, entry, exit, fill, fee, or ranking logic;
- run the friend's algorithm;
- add PSX timezone/calendar behavior;
- enable Celery Beat or inherited US-market schedules;
- implement current quotes;
- perform Schwab OAuth;
- place paper or live broker orders;
- add live-trading credentials;
- build a production deployment;
- install nginx/systemd services on the host;
- clean or reorganize unrelated repository files;
- commit or push unless the user separately asks.

These belong to C3 and later contracts.

## 5. Required Deliverables

The exact filenames may follow repository conventions, but the delivery must
include:

1. Pinned dependency source and lock files.
2. Development Dockerfile.
3. `.dockerignore` additions if required.
4. Development Compose file.
5. Sanitized environment template.
6. Idempotent disposable database bootstrap, if startup requires one.
7. Smoke/unit tests or a deterministic verification script.
8. Local-development documentation.
9. `DELIVERY.md` for C2.

`DELIVERY.md` must contain:

- files added and changed;
- pre-existing worktree changes that were preserved;
- dependency/version choices and evidence;
- exact build, startup, test, and shutdown commands;
- test results, including commands and concise output;
- unverified acceptance criteria;
- startup side effects discovered;
- deviations from this contract and why;
- known limitations and recommended C3 prerequisites.

## 6. Acceptance Criteria

C2 is accepted only when all applicable criteria below are demonstrated:

- [ ] A clean container build installs only from pinned dependency metadata.
- [ ] The documented Python version matches the image and lock.
- [ ] Compose validates without missing-variable surprises.
- [ ] Default Compose startup includes only `web` and `postgres`.
- [ ] The application starts against an empty disposable PostgreSQL database.
- [ ] Database initialization is repeatable without error or duplicate data.
- [ ] `/healthz` returns the expected successful response.
- [ ] A representative rendered page and static asset are accessible.
- [ ] Startup requires no Redis, Celery, Beat, Schwab token, OAuth, PSX DB, or
      external API.
- [ ] The optional worker profile is isolated and explicitly invoked.
- [ ] Celery Beat is not started.
- [ ] Effective configuration disables live trading and enables the emergency
      stop.
- [ ] No production credentials or secret values are committed or printed in
      delivery evidence.
- [ ] Tests provide evidence that startup does not contact Schwab/broker hosts.
- [ ] Existing unrelated worktree changes remain untouched.
- [ ] Documentation can be followed from a fresh checkout with Docker/Compose.
- [ ] All failures, skipped checks, and environmental blockers are stated
      plainly; no partial result is reported as fully verified.

## 7. Execution Rules

- Work only in the Stockwicks application repository.
- Treat the existing worktree as user-owned and preserve unrelated changes.
- Use read-only inspection before modifying overlapping configuration files.
- Use parameterized/sanitized configuration; never expose secret values in
  command output or documentation.
- Do not run production setup/deployment scripts.
- Do not connect to production databases or external broker endpoints.
- Do not start inherited scheduled tasks.
- Prefer focused, reversible changes.
- Do not silently weaken tests, health checks, authentication, or trading safety
  gates to make the runtime start.
- When evidence contradicts C1, follow repository evidence and document the
  correction.
- If satisfying C2 requires changes to market-data or strategy behavior, stop
  and propose moving that work to C3/C4 rather than expanding scope.

## 8. Executioner Handoff Prompt

```text
Implement Contract C2: Reproducible Local Development Runtime.

Read the complete C2 contract and the C1 delivery before making changes. Work in
the inherited Stockwicks repository only. Preserve all unrelated dirty-worktree
changes and stop if a required target file overlaps with uncommitted user work.

Build a pinned, containerized reduced runtime with FastAPI/Uvicorn and disposable
PostgreSQL. Redis and a Celery worker must be optional. Do not start Celery Beat.
Keep trading and external broker access disabled. Do not implement PSX data or
strategy integration; that work starts in C3.

Verify the result from a clean disposable state. Provide evidence for the health
endpoint, a rendered page, a static asset, repeatable database bootstrap, service
profile isolation, safety flags, and absence of Schwab/broker network calls.

Do not commit or push. Create a detailed C2 DELIVERY.md with exact commands,
results, changed files, preserved pre-existing changes, deviations, limitations,
and any acceptance criteria that could not be verified.
```
