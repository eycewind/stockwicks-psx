# Runtime Architecture

## Verified current topology

```text
Browser/API
  -> FastAPI web
     -> PostgreSQL (users, bots, Replay, Sparkie, trades)
     -> read-only PSX SQLite or Schwab market data
     -> optional Redis/Celery task queue
     -> local volumes (data, logs, models)

Celery worker (optional profile)
  -> replay/default queues in compose.dev.yml
  -> Replay task -> detached Python orchestrator subprocess
  -> Sparkie/optimizer tasks

Production-oriented celery_worker.py
  -> stock, replay, broker, default queues
  -> beat schedules for US sessions, token refresh, risk, cleanup
```

`compose.dev.yml` has web/PostgreSQL by default and optional Redis/worker profile. The worker command consumes `default,replay`; it does not run the stock or broker queues. This reduced local runtime intentionally cannot represent the full production lifecycle.

## Lifecycles

- Stock bot: route creates/configures DB row → start task marks RUNNING → beat queries active bots by interval → runner applies emergency/paper/risk gates → strategy tick → paper trade and optional live mirror. Redis stop flags and DB status support stop/recovery.
- Replay: route validates/stores session and ingests data → Celery task (or local fallback) calls `replay_process.start_session` → detached orchestrator constructs `ReplayDataProvider`, loops bars, calls replay tick, writes cursor/status, polls stop, cleans dangling position/model cache.
- Sparkie: API writes job → Celery/background execution evaluates candidates → stores events/results → queues exact Replay finalist verification → user may create a paper bot after gates/acknowledgements.
- Paper/live: strategy calls a combined trade service. Paper writes DB; live mirror resolves Schwab account/token and submits order only when gates and bot flags allow.

## Scheduling and assumptions

`app/celery_worker.py` runs in America/New_York and schedules 1–30 minute ticks, a 16:10 daily tick, Schwab token refresh, P&L checks, and 15:58 EOD close. These are active production-oriented definitions but not enabled by the reduced Compose worker profile.

## Operations

Logging is split among app logs, per-bot JSONL/text, DB error/status fields, Sparkie events, and audit events. No unified metrics/tracing/SLO surface was proved. Replay PID liveness is disabled in web-only development and enabled in worker. Detached processes complicate ownership and container reaping; a future PSX runtime should use one durable job owner and idempotent leases/heartbeats.

## Dead/duplicate risk

There are duplicate Replay route files, old runners, copied backtests, and direct network helpers. Only imports/mounts prove activity. A removal contract should add import/route coverage before deleting them.
