# Evidence Index

All paths are repository-relative. “Verified” means directly traced in code/config/schema; it does not mean runtime profitability or production readiness.

| ID | Evidence | Supports |
|---|---|---|
| E01 | `app/main.py` router imports/includes | active product surfaces |
| E02 | `app/routes/paper_trade_bot.py:ALLOWED_MM_ALGOS` | Algo1–5 UI mapping |
| E03 | `app/trading/runners/stock_bot_runner.py:run_stock_bot_tick` | active dispatch and safety gates |
| E04 | `app/tasks/stock_tasks.py:run_stock_bots_for_interval` | scheduler lifecycle and US sessions |
| E05 | `app/scripts/stock_algos/Algo1_MM.py` … `Algo5_MM.py` | model decisions, defaults, execution wiring |
| E06 | `app/scripts/research/Featureset_1.py` … `Featureset_5.py` | feature lists, OHLCV requirements, Schwab heritage |
| E07 | `app/scripts/ml/mm_live_helpers.py` | HGB training/joblib/inference |
| E08 | `app/scripts/ml/model_refresh_policy.py` | artifact loading/retraining policy |
| E09 | `app/services/mm_core_engine.py` | directional entry/exit/risk logic; not market making |
| E10 | `app/scripts/stock_algos/algoMM_replay_runner.py` | Replay strategy mapping and inference |
| E11 | `app/modules/replay/routes.py` | active Replay API/start/ingestion |
| E12 | `app/tasks/replay_tasks.py`, `app/services/replay_process.py`, `app/scripts/replay/orchestrator.py` | Replay queue/subprocess lifecycle |
| E13 | `app/scripts/replay/replay_data_provider.py` | Replay bar cache/resampling |
| E14 | `app/market_data/*`, `app/config.py` | C3 PSX provider and configuration |
| E15 | `compose.dev.yml` | reduced services, safety flags, volumes, read-only PSX mount |
| E16 | `app/celery_worker.py`, `app/celery_app.py` | queues, beat schedules, timezone |
| E17 | `app/routes/sparkie.py`, `app/tasks/sparkie_tasks.py` | Sparkie API, evaluation, Replay and bot gates |
| E18 | `app/services/sparkie_engine.py`, `sparkie_weekly_service.py`, `sparkie_risk_scoring.py` | candidate research/ranking |
| E19 | `app/models/sparkie.py` | Sparkie persisted state |
| E20 | `app/models/replay.py`, `paper_trading.py`, `paper_trading_bot.py` | sessions/orders/trades/positions |
| E21 | `app/models/schwab.py`, `app/modules/broker/routes.py`, `app/routes/schwab_trade.py` | broker/OAuth/account/order boundaries |
| E22 | `app/services/trade_service.py`, `paper_trade_service.py` | combined paper/live/notification path |
| E23 | `app/services/email_service.py` | SMTP side effects and notification log |
| E24 | `app/services/barchart_symbols.py`, `stocktwits_symbols.py` | US universe dependencies |
| E25 | `app/scripts/stock_algos/AI_algo2_MACD.py` | misleading AI name; deterministic SMI |
| E26 | `app/scripts/stock_algos/adaptive_predictor.py` | deterministic adaptive voting |
| E27 | `app/scripts/stocks/backtest_algos/*` | experimental ML/evaluator duplicates |
| E28 | `app/routes/spx_0dte_routes.py`, `spx_0dte_trades.py`, `app/scripts/options/*` | US options domain |
| E29 | `contacts/C3_read_only_PSX_market_data_integration/{CONTRACT,DELIVERY}.md` | accepted C3 baseline |

## Unresolved evidence

- Deployed joblib provenance and training windows: artifacts are not tracked.
- Production process supervisor/topology beyond repository configuration.
- StockIntel schemas, entitlement, retention license, sequences, and UAT behavior.
- PSX broker APIs and exact fee/settlement/short/lot/price-band rules.
- Whether every legacy/copy script is operationally unused outside this repository.
