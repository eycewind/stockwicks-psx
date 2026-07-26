# Product Surface

| Surface | Verified flow | Data/state | Side effects | Status |
|---|---|---|---|---|
| Login/account | `/auth/*`, `/account` → user services/models | PostgreSQL `users` | email verification/recovery | Active |
| Dashboard | `/dashboard` → templates | user/account summaries | none on GET | Active |
| Broker setup/trading | `/broker/*`, `/trade/*`, Schwab routes → broker/token utilities | `broker_connections`, `schwab_accounts`, encrypted tokens | OAuth and Schwab account/order APIs | Active, US-specific |
| Manual paper trading | `/auth/papertrading/*` → paper services | `paper_accounts`, `paper_orders`, `paper_trades` | quote reads; DB writes | Active |
| Stock bot | `/auth/papertradebot/*` → stock tasks → `stock_bot_runner` → strategy | bot/open/history tables, JSONL logs, model files | paper DB, email; optional Schwab order | Active |
| Replay | `/replay-simulator/*` → Replay route → replay task/process → orchestrator → replay runner | sessions/open/history, Replay CSV cache, model cache | local DB/files/process | Active; PSX daily supported |
| Strategy optimizer / cheatsheet | `/analysis/strategy-optimizer/*` → optimizer tasks/service | run artifacts/results | task queue and files/DB | Active surface; US defaults remain |
| Sparkie | `/auth/sparkie`, `/api/sparkie/*` → Sparkie tasks/services → optimizer/Replay/bot setup | Sparkie job/candidate/event/weekly tables | queue, subprocess, possible bot creation | Active; redesign for PSX |
| Research lists | `/api/research/stocks/*` → Barchart service | remote/snapshot symbol lists | HTTP reads, optional CSV refresh | Active, US-specific |
| Log analysis | `/analysis/logs*` → log analysis service | JSONL/text logs | none beyond reads | Active |
| SPX 0DTE | `/options/spx-0dte*` → SPX runner/services | SPX pick/open/history tables | Schwab option-chain reads, email | Active routes, irrelevant to PSX equities |
| Admin live trades | `/admin/live-trades*` | bot/broker state | potentially trade administration | Active and privileged |
| Legacy Replay | `app/routes/replay.py` | duplicates active module | ambiguous | Not mounted; active import is `app.modules.replay.routes` |
| Scripts/backtests | `app/scripts/**` command entry points | CSV/model/log artifacts | several contain network/order-capable code | Experimental; not web surface unless imported |

## Active router evidence

`app/main.py` mounts user, page, dashboard, broker, paper bot, active module Replay, analysis, admin live trade, SPX, Sparkie, research, Schwab trade/UI/auth/history/API routers. A filename alone is not treated as active; mount/import and call tracing are required.

## Feature flags

`app/config.py` defines `TRADING_ENABLED`, `PAPER_TRADING_ENABLED`, `LIVE_TRADING_ENABLED`, `EMERGENCY_STOP`, `MARKET_DATA_PROVIDER`, and PSX settings. `compose.dev.yml` disables trading and live/paper execution and enables emergency stop. These controls are deployment safety gates, not a substitute for separating strategy from execution.
