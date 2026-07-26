# Data and Database Map

## Market-data levels

| Level | Existing source/store | Writer | Readers | PSX status |
|---|---|---|---|---|
| Daily OHLCV | read-only PSX SQLite; adjusted/raw mode | external PSX pipeline | C3 provider, Replay/base wiring | Available |
| Intraday OHLCV | Schwab price history; Replay CSV cache | remote fetch/ingest | live algos, Replay | Not available for PSX |
| Trades/ticks | no canonical PSX store proved | — | some future strategies may need | Missing |
| L0 snapshot | no normalized PSX store | — | future viewer/scanner | Missing |
| L1/top of book | no normalized store | — | execution/spread logic | Missing |
| L2 depth/events | no normalized store | — | order-book strategies | Missing |
| App orders | `paper_orders`; Schwab API order state | app/broker | paper/live UI/services | Not market depth |
| Fills/trades | paper/replay/bot/SPX tables; broker responses | app/broker | UI/risk/reporting | Not exchange feed |
| Positions/balances | paper account/trade and bot-open tables; broker accounts | app/broker | execution/risk/UI | Application state |
| Derived state | model files, candidate/results, logs, rankings | strategies/services | Replay/Sparkie/UI | Must be versioned |

## PostgreSQL application tables

- Identity/integration: `users`, `broker_connections`, `schwab_accounts`, token-related legacy models.
- Manual paper: `paper_accounts`, `paper_orders`, `paper_trades`.
- stock bots: `paper_stock_trade_bots`, `paper_stock_bot_open_trades`, `paper_stock_bot_trade_history`.
- Replay: `replay_sessions`, `replay_open_trades`, `replay_trade_history`.
- Sparkie: jobs, candidates, events, weekly runs/results/schedules.
- Options: SPX picks/open/history and alert subscriptions.
- Operations: `audit_events`, `trade_notification_log`.

Timestamps are mostly naive `datetime.utcnow`; strategy code frequently converts to `US/Eastern`. This mixed convention is unsafe. Target storage should use timezone-aware UTC, with explicit exchange session date and source timezone.

## Files, caches, and queues

- PSX SQLite: configured `PSX_DB_PATH`, mounted read-only in development. It is source market data, not app state.
- Replay CSV: generated cache named `{symbol}_{interval}.csv` beneath configured data space. Rebuildable; should not be committed.
- Models: joblib packages beneath `MODEL_DIR`. Derived, version-sensitive artifacts.
- Logs: JSONL/text beneath `LOG_DIR`; operational evidence, not source data.
- Redis/Celery: transient task transport and stop/running flags; not authoritative durable state.
- PostgreSQL: authoritative workflow, portfolio, and application state.

Retention is not centrally specified for models, logs, Replay CSV, Sparkie events, or future feed data (**Unresolved**). C4 recommends explicit retention and raw-feed sizing in the collector contract.

## Strategy minimums

Algo1–5, SMI, and MACD can compute from OHLCV; PSX daily data is technically sufficient only for daily variants. Intraday configurations require intraday bars. Realistic execution needs at least timestamped trades/L1 and exchange rules; L2 strategies require depth/event history. Options modules require option chains/Greeks/OI and are not supported by the PSX daily database.
