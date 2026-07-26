# C4 Delivery

## Status

Complete.

## Scope delivered

C4 produced an evidence-led, documentation-only audit under `docs/c4_stockwicks_audit/`. It maps product surfaces, canonical strategies and bots, ML/AI behavior, databases/data levels, runtime lifecycles, integrations, market assumptions, PSX dispositions, a target PSX architecture, and a dependency-ordered roadmap.

No trading behavior, strategy parameter, database schema, Replay implementation, broker integration, Signal Viewer integration, or StockIntel connectivity was changed. The StockIntel trial was not activated. No broker, email, or external-data diagnostic was executed.

## Key conclusions

- “MM” is a directional probability strategy label, not market making.
- Algo1–5 use real scikit-learn model inference but require PSX-specific datasets, provenance, chronology tests, and retraining.
- Sparkie is research/selection orchestration rather than a strategy and requires PSX redesign.
- C3 daily history and Replay are the strongest direct reuse candidates.
- Daily OHLCV cannot validate intraday, L1/L2, liquidity, or realistic execution behavior.
- The first post-C4 dependency is a safe StockIntel collector/raw-capture contract before trial activation.

## Deliverables

- `00_EXECUTIVE_SUMMARY.md`
- `01_PRODUCT_SURFACE.md`
- `02_STRATEGY_CATALOGUE.md`
- `03_ML_AI_AUDIT.md`
- `04_DATA_AND_DATABASE_MAP.md`
- `05_RUNTIME_ARCHITECTURE.md`
- `06_EXTERNAL_INTEGRATIONS.md`
- `07_ASSUMPTION_REGISTER.md`
- `08_PSX_DISPOSITION_MATRIX.md`
- `09_TARGET_PSX_ARCHITECTURE.md`
- `10_RECOMMENDED_ROADMAP.md`
- `EVIDENCE_INDEX.md`

## Verification

- Full configured discovery (stockwicks-local Conda interpreter):
  `python -m pytest -q -s`
  → **40 passed, 50 inherited deprecation warnings in 2.26s**.
- `docker compose --env-file .env.dev -f compose.dev.yml config -q`
  → **passed**.
- `git diff --check`
  → **passed**.
- Artifact scan of the C4 directories found no CSV, SQLite/database, joblib, log, or temporary files.

The first sandboxed test attempt used `python -m pytest -q`, but `python` was
not on that shell's PATH. A second sandboxed run with the explicit Conda
interpreter reached the Docker-backed C2 runtime test and stalled because the
sandbox could not access the local Docker stack; a 90-second diagnostic run
identified `tests/test_c2_reduced_runtime.py::test_startup_health_login_and_static_without_network`.
The final suite was run with approved local-stack access. Pytest capture was
disabled (`-s`) because the host execution wrapper removed its capture
temporary file during an initial approved run; this did not affect test
selection or results.

## Inspection commands

The audit used read-only `rg`, `find`, `sed`, `git status`, and `git diff`
queries over:

- mounted FastAPI routers and route decorators;
- stock, Replay, optimizer, Sparkie, broker, and Celery entry points;
- SQLAlchemy model/table declarations;
- strategy dispatch, features, model training/loading, and common decisions;
- Compose/runtime configuration and safety flags;
- external HTTP, broker, OAuth, email, and notification call sites;
- serialized-artifact filenames and repository contents.

No historical strategy, broker, notification, or external-feed component was
executed.

## Limitations and unresolved items

- No deployed joblib artifact is tracked, so deployed model provenance,
  training dates, metrics, and reproducibility remain unresolved.
- Production supervisor/topology outside repository configuration was not
  inspected.
- StockIntel schemas, sequence behavior, UAT facilities, entitlement,
  licensing, and retention terms remain unresolved because C4 deliberately
  made no connection.
- Exact PSX broker, settlement, fee/tax, short-sale, lot, price-band, auction,
  and partial-fill rules require authoritative inputs in later contracts.
- Static tracing cannot prove that no external operator invokes every legacy
  standalone script; files were classified by proved repository entry points.

## Acceptance evidence

All twelve required audit files exist. Material conclusions map to repository
artifacts in `docs/c4_stockwicks_audit/EVIDENCE_INDEX.md`. The working-tree
diff contains documentation only, existing configured tests pass, Compose
validates, and diff hygiene passes.

## Repository actions

No commit, push, merge, or pull request was performed.
