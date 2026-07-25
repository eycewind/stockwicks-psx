# C2 Delivery — Reproducible Local Development Runtime

Date: 2026-07-25  
Branch: `c2-reproducible-local-runtime`  
Baseline commit: `75ee8fe`  
Repository: `/home/hassan/ashakil`

## Outcome

C2 is implemented and its runtime verification is accepted. Verification
covered clean locked installation, a no-cache image build, Compose topology,
disposable PostgreSQL initialization, schema idempotence, HTTP paths, effective
safety state, and the optional Redis/Celery profile.

No C2 files have been committed or pushed. Pre-existing user-owned worktree
changes were not restored, deleted, staged, reformatted, or overwritten.

## Complete C2 file inventory

### Added source-controlled delivery files

- `.dockerignore` — excludes secrets, local environments, databases, caches,
  generated root runtime state, keys, contracts, and deployment-only material
  from the image context while retaining application source such as
  `app/models`.
- `Dockerfile.dev` — Python 3.12.3 non-root development image, hashed-lock
  installation, Uvicorn command, writable paths, and web healthcheck.
- `LOCAL_DEVELOPMENT.md` — developer setup, startup, verification, profile,
  lock-regeneration, shutdown, and precise disposable-volume reset guidance.
- `compose.dev.yml` — default `web`/`postgres` topology and explicit optional
  `redis`/`worker` profile.
- `env.dev.example` — sanitized local configuration template.
- `requirements/runtime.in` — exact direct runtime dependency pins.
- `requirements/runtime.lock` — generated and fully hashed transitive runtime
  dependency graph.
- `requirements/dev.in` — separate exact test/tooling dependency input.
- `requirements/dev.lock` — generated and fully hashed transitive development
  dependency graph.
- `scripts/dev_bootstrap.py` — idempotent ORM-based disposable PostgreSQL
  bootstrap.
- `scripts/verify_dev_runtime.py` — HTTP health, login HTML, and static-asset
  verifier.
- `tests/test_c2_reduced_runtime.py` — reduced-mode safety, startup,
  network-guard, portal, and static-asset tests.
- `tests/test_c2_compose_contract.py` — Compose topology, profile, safety, Beat,
  port, worker-healthcheck, and PSX-isolation assertions.
- `contacts/C2_reproducible_local_development_runtime/DELIVERY.md` — this
  delivery record.

The user supplied
`contacts/C2_reproducible_local_development_runtime/CONTRACT.md`; it was read but
not created or modified by C2 execution.

### Modified existing application files

- `app/config.py` — added `reduced_local_runtime`, mapped to
  `REDUCED_LOCAL_RUNTIME`, with a production-preserving default of `false`.
- `app/main.py` — added the narrow startup guard that, only in reduced local
  mode, skips the SPX 0DTE and Sparkie feature-schema startup helpers. Existing
  route registration and normal configured production behavior remain intact.

### Generated local-only file

- `.env.dev` — ignored local configuration containing generated development-only
  secrets. It is not a source-controlled delivery file, and no value from it is
  included in this report.

## Exact verification commands used

Commands below are reproduced without secret values.

### Lock installation

Final clean development-lock installation and test environment:

```bash
python3 -m venv /tmp/stockwicks-c2-final-test
/tmp/stockwicks-c2-final-test/bin/python -m pip install --require-hashes -r requirements/dev.lock
```

The image installed the runtime lock through the Dockerfile command:

```bash
python -m pip install --require-hashes --no-deps -r /tmp/runtime.lock
```

### No-cache image build

```bash
docker compose --env-file .env.dev -f compose.dev.yml build --no-cache web
```

### Compose validation and service-list checks

```bash
docker compose --env-file .env.dev -f compose.dev.yml config --quiet
docker compose --env-file .env.dev -f compose.dev.yml config --services
docker compose --env-file .env.dev -f compose.dev.yml --profile worker config --services
```

Observed default list:

```text
postgres
web
```

Observed explicit-profile list:

```text
postgres
redis
web
worker
```

### Fresh PostgreSQL/default startup

The named C2 PostgreSQL volume did not exist before this startup; Compose
reported creating `stockwicks-c2-postgres-data`.

```bash
docker compose --env-file .env.dev -f compose.dev.yml down --remove-orphans
docker compose --env-file .env.dev -f compose.dev.yml up --build -d
```

After narrowing `.dockerignore`, the corrected image/default stack was recreated
with:

```bash
docker compose --env-file .env.dev -f compose.dev.yml down --remove-orphans
docker compose --env-file .env.dev -f compose.dev.yml build --no-cache web
docker compose --env-file .env.dev -f compose.dev.yml up -d
```

### Bootstrap and idempotence runs

The web service also ran this command automatically before Uvicorn. It was then
run twice explicitly:

```bash
docker compose --env-file .env.dev -f compose.dev.yml exec -T web python -m scripts.dev_bootstrap
docker compose --env-file .env.dev -f compose.dev.yml exec -T web python -m scripts.dev_bootstrap
```

Both explicit runs reported 24 tables. The final database count was checked
with:

```bash
docker compose --env-file .env.dev -f compose.dev.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select count(*) from information_schema.tables where table_schema = '\''public'\'';"'
```

### HTTP verification

```bash
python3 scripts/verify_dev_runtime.py
```

This checked:

- `/healthz` returned HTTP 200 and the confirmed effective JSON body;
- `/auth/login` returned rendered HTML successfully;
- `/static/css/style.css` returned HTTP 200 with content.

### Effective safety-state inspection

```bash
docker compose --env-file .env.dev -f compose.dev.yml exec -T web python -c 'import os; required={"TRADING_ENABLED":"false","PAPER_TRADING_ENABLED":"false","LIVE_TRADING_ENABLED":"false","EMERGENCY_STOP":"true","REDUCED_LOCAL_RUNTIME":"true","SCHWAB_CLIENT_ID":"","SCHWAB_CLIENT_SECRET":""}; actual={k:os.getenv(k) for k in required}; assert actual==required, actual; print("container safety state passed")'
```

This command checks names and required safe values only; it does not display or
inspect generated application/database secrets.

### Worker-profile startup and Celery ping

```bash
docker compose --env-file .env.dev -f compose.dev.yml --profile worker up -d --build redis worker
docker compose --env-file .env.dev -f compose.dev.yml exec -T worker celery -A app.celery_app:celery_app inspect ping --timeout 8
```

After correcting the inherited web healthcheck on the worker, it was recreated
and inspected with:

```bash
docker compose --env-file .env.dev -f compose.dev.yml --profile worker up -d --force-recreate worker
docker top stockwicks-c2-dev-worker-1 -eo pid,user,args
```

The worker returned one `pong`, ran as UID 10001, used only
`default,replay`, and its process command contained no Beat invocation.

### Tests

Final exact test run:

```bash
/tmp/stockwicks-c2-final-test/bin/python -m pytest -s -q --disable-warnings tests/test_c2_reduced_runtime.py tests/test_c2_compose_contract.py
```

Result:

```text
4 passed, 44 warnings in 1.23s
```

### Optional-container removal

```bash
docker compose --env-file .env.dev -f compose.dev.yml --profile worker stop worker redis
docker compose --env-file .env.dev -f compose.dev.yml --profile worker rm -f worker redis
docker compose --env-file .env.dev -f compose.dev.yml ps --services --filter status=running
```

At verification time, the final running-service list returned to:

```text
postgres
web
```

### Final git status command

```bash
git status --short
```

## C2 acceptance checklist

### T1 — Repository and runtime preflight

- **PASS** — C1 contract/delivery and C2 contract were reviewed.
- **PASS** — branch, baseline commit, Python version, application entry point,
  configuration loader, database behavior, and dependency state were recorded.
- **PASS** — pre-existing modified, deleted, and untracked items were identified.
- **PASS** — unrelated user changes were preserved.
- **PASS** — no existing C2 target contained overlapping user edits before C2
  modification.

### T2 — Pinned Python dependency definition

- **PASS** — every dependency required by the tested reduced web and optional
  worker runtime is directly pinned or transitively locked.
- **PASS** — runtime and development dependencies are separated.
- **PASS** — locks are generated and fully hashed.
- **PASS** — Python 3.12.3 is documented and used by the image/checks.
- **PASS** — dependencies were refined from imports and real clean-startup
  failures (`email-validator`).
- **PASS** — the `pandas-ta`/NumPy/Numba constraint was resolved explicitly.
- **PASS** — no credential, local path, environment, or cache is in a lock.
- **PASS** — exact lock-regeneration commands are documented.

### T3 — Development container image

- **PASS** — pinned Python 3.12.3 base image.
- **PASS** — hashed runtime-lock installation completed in a no-cache build.
- **PASS** — runtime user is non-root (`stockwicks`, UID/GID 10001).
- **PASS** — required application sources/assets are copied.
- **PASS** — writable `data`, `logs`, and `models` paths exist/mount.
- **PASS** — port 8101 is exposed and Uvicorn starts `app.main:app`.
- **PASS** — `/healthz` container healthcheck reached healthy state.
- **PASS** — secrets, env files, databases, caches, logs, model outputs, and
  local environments are excluded from the image context.
- **PASS** — image startup does not run destructive setup, deployment services,
  Beat, OAuth refresh, or live trading.

### T4 — Development Compose topology

- **PASS** — default services are exactly `web` and `postgres`.
- **PASS** — `redis` and `worker` require explicit `worker` profile activation.
- **PASS** — Beat is omitted.
- **PASS** — web waits for healthy PostgreSQL.
- **PASS** — worker waits for healthy PostgreSQL and Redis.
- **PASS** — disposable PostgreSQL and runtime paths use named volumes.
- **PASS** — web binds to localhost; PostgreSQL/Redis ports are not published.
- **PASS** — future market-data configuration seam exists without a C2 PSX
  service.
- **PASS** — no real PSX SQLite database/path is mounted or referenced.
- **PASS** — development startup failures are not hidden by restart policies.

### T5 — Sanitized configuration and safety gates

- **PASS** — `env.dev.example` contains safe placeholders/names only.
- **PASS** — app environment, port, generated-secret placeholders, PostgreSQL,
  optional Redis/Celery, runtime directories, trading gates, and future provider
  seam are documented.
- **PASS** — effective container state has trading and live trading disabled,
  emergency stop enabled, and paper trading disabled.
- **PASS** — no real Schwab/provider credentials or production database/order
  endpoint is present.
- **PASS** — no inherited production fallback behavior was broadened or changed;
  Compose supplies safe development values.

### T6 — Disposable PostgreSQL bootstrap

- **PASS** — ORM metadata, initialization helpers, and startup side effects were
  inspected first.
- **PASS** — bootstrap uses the current ORM model registry and `checkfirst=True`.
- **PASS** — startup used a new named development volume, not a production
  database.
- **PASS** — no production/client data was copied and no account was seeded.
- **PASS** — bootstrap succeeded from empty PostgreSQL and twice consecutively.
- **PASS** — 24 public model tables were confirmed.
- **PASS** — migration debt and unverified feature-specific schema completeness
  are documented without introducing Alembic.

### T7 — Reduced-mode feature isolation

- **PASS** — startup and `/healthz` require no Schwab, broker, OAuth, email,
  market-data, or model-provider request.
- **PASS** — no Beat task/process starts.
- **PASS** — default startup does not start a worker.
- **PASS** — unsupported routes remain registered without import/startup calls
  to external providers.
- **PASS** — production behavior is preserved when the reduced flag is absent.
- **PASS** — the narrow `REDUCED_LOCAL_RUNTIME` flag is documented and tested.
- **PASS** — application modules were not broadly deleted or stubbed.

### T8 — Smoke tests and verification

1. **PASS** — locked dependencies installed in a clean host environment and a
   no-cache image build.
2. **PASS** — Compose configuration validated.
3. **PASS** — web/PostgreSQL started from a clean disposable state.
4. **PASS** — PostgreSQL became healthy.
5. **PASS** — `/healthz` returned HTTP 200 and expected JSON.
6. **PASS** — `/auth/login` returned successful rendered HTML.
7. **PASS** — `/static/css/style.css` returned HTTP 200.
8. **PASS** — bootstrap succeeded from empty PostgreSQL.
9. **PASS** — bootstrap succeeded twice consecutively.
10. **PASS** — default startup service list excludes Redis, worker, and Beat.
11. **PASS** — Redis/worker started only through the explicit profile and Celery
    answered `pong`.
12. **PASS** — startup/tests used blank Schwab credentials.
13. **PASS** — the network-guarded startup/HTTP test recorded no Schwab or broker
    request.
14. **PASS** — required trading safety flags were asserted inside running web.

### T9 — Developer documentation

- **PASS** — prerequisites and sanitized-template copying are documented.
- **PASS** — development-secret generation is documented without fixed secrets.
- **PASS** — default build/start, health, logs, optional profile, stop, tests,
  precise database reset, and lock regeneration are documented.
- **PASS** — known limitations and migration debt are documented.
- **PASS** — documentation explicitly states that PSX integration is not C2.

### Explicit out-of-scope compliance

- **PASS** — C2 did not implement/mount/migrate PSX data or a PSX API.
- **PASS** — C2 did not change OHLC, strategy, indicator, signal, order, fill,
  fee, ranking, calendar, or timezone semantics.
- **PASS** — C2 did not perform OAuth, contact a live broker, start Beat, place
  orders, add live credentials, or create a production deployment.
- **PASS** — unrelated repository files were not cleaned/reorganized.
- **PASS** — C2 has not been committed or pushed.

No C2 acceptance criterion is marked FAIL.

## Final concise git status and ownership separation

The final command was `git status --short`.

### C2 changes

```text
 M app/config.py
 M app/main.py
?? .dockerignore
?? Dockerfile.dev
?? LOCAL_DEVELOPMENT.md
?? compose.dev.yml
?? contacts/C2_reproducible_local_development_runtime/
?? env.dev.example
?? requirements/
?? scripts/
?? tests/
```

Within the untracked C2 contract directory, `CONTRACT.md` is user-supplied and
`DELIVERY.md` is C2-authored. The complete delivered files under collapsed
untracked directories are listed in the inventory above.

### Pre-existing user-owned changes, preserved separately

```text
 D .gitattributes
 D .github/workflows/ci.yml
 D .github/workflows/deploy-ashakil.yml
 D .github/workflows/deploy-rollout.yml
 D .github/workflows/rollback-ashakil.yml
 D .github/workflows/rollback-rollout.yml
 D .github/workflows/rollback-to-ref.yml
 D .github/workflows/ssh-test.yml
 M .gitignore
 D README.md
 D cd
 D deploy/rollback_ashakil.sh
 D dir
?? .tmp-admin-main/
?? .tmp-main-clone/
?? .tmp-main-merge-20260714/
?? Miniconda3-latest-Linux-x86_64.sh
?? app/templates/broker/Charlesschwab.jpg
?? app/templates/broker/InteractiveBrokers.jpg
?? app/templates/broker/interactivebroker.png
?? app/templates/broker/tradier.jpg
?? app/trading/data/
?? contacts/C1_application_discovery/C1_CONTRACT.md:Zone.Identifier
?? requirements-local.txt
```

The ignored `.env.dev` is generated C2 runtime state, not a user-owned change or
source-controlled delivery file.

## Contract deviation statement

C2 did **not materially deviate from the contract**. Implementation choices were
within the contract's permitted options:

- requirements input files plus generated hashed locks were selected;
- ORM `create_all(checkfirst=True)` was selected for disposable development
  bootstrap instead of introducing migration history;
- a narrow reduced-mode flag was added with production-preserving default;
- the sanitized template is named `env.dev.example` instead of a dot-prefixed
  variant because the pre-existing user-owned `.gitignore` intentionally ignores
  `.env.*` files;
- paper trading is disabled because C2 does not exercise scheduled/broker paper
  actions.

Two defects found only through Docker verification were corrected without scope
expansion: `.dockerignore` initially excluded `app/models`, and the worker
initially inherited the web healthcheck. Neither correction changes application
or trading semantics.

## Runtime preservation note

No build, start, stop, restart, removal, commit, push, or other lifecycle command
was issued while performing this DELIVERY-only update. A read-only

```bash
docker compose --env-file .env.dev -f compose.dev.yml ps
```

at documentation-capture time returned no service rows. Therefore the earlier
accepted verification that ended with healthy running `web`/`postgres` remains
valid evidence, but this report does not falsely claim those containers are
currently running. The documentation update did not cause or alter that external
runtime state.
