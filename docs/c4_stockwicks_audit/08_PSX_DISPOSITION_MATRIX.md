# PSX Disposition Matrix

| Component | Disposition | Input / evidence | Coupling and risk | Value / effort / next action |
|---|---|---|---|---|
| C3 PSX SQLite provider | Reuse directly | adjusted daily OHLCV; `app/market_data` | strict daily only | High / S / retain read-only contract |
| Replay UI/provider lifecycle | Extract and adapt | bars + DB state | subprocess/CSV/model coupling | High / M / define job and data ports |
| Signal Viewer | Integrate through SignalIntent contract | generated signals; viewer is external to the audited repository | direct reuse was not established by C4 | High / S-M / later signal contract |
| MM common decision concepts | Extract and adapt | calibrated probabilities/portfolio | USD/EOD/short assumptions | Medium / M / pure strategy intent interface |
| Algo1 | Extract and adapt | OHLCV | model/provenance absent | Medium / M / chronology tests + PSX retrain |
| Algo2 | Redesign for PSX | OHLCV/intraday for meaningful VWAP | daily VWAP proxy | Medium / M | define feature semantics |
| Algo3/SMI | Extract and adapt | OHLCV | thresholds/timeframe | Medium / M | PSX retrain/recalibrate |
| Algo4 | Reject/consolidate | duplicates Algo1 features | divergent implementation | Low / M | prove parity then retire |
| Algo5/MACD | Extract and adapt | OHLCV | timeframe/calibration | Medium / M | PSX retrain |
| Legacy fallback runners/copies | Reject | ambiguous imports | unsafe duplication | Low / M | inventory tests then remove |
| Sparkie orchestration | Redesign for PSX | universe, backtests, Replay | US sources/USD goals/live setup | High / L | rebuild after engine/interfaces |
| Barchart/Stocktwits discovery | Reject | US remote rankings | wrong universe | None / S | use PSX instrument master/scanner |
| Intraday strategies | Defer pending intraday/L1 history | intraday OHLCV | no PSX history | Potential / L | collector first |
| Spread/order-book strategies | Defer pending L1/L2 history | quotes/depth/events | no store/execution model | Potential / L | capture and replay feed |
| Paper trading DB/UI | Redesign for PSX | intents, quotes, fees | simplistic fills/USD | High / L | execution contract |
| Schwab market/broker adapters | Reject for PSX, retain as reference | US APIs | deep coupling | Low / L | new adapter port |
| SPX/options suite | Reject for PSX equity MVP | chains/Greeks/OCC | product irrelevant | None / L | isolate |
| Auth/users/audit patterns | Reuse directly | app state | generic | High / S | retain |
| Celery/Redis | Extract and adapt | jobs/events | queue ownership complexity | Medium / M | durable idempotent jobs |
| JSONL/Sparkie event logging | Extract and adapt | events | fragmented observability | Medium / S-M | common audit schema |
| Local LLM analysis | Unresolved | missing artifact/deployment proof | hardcoded path | Low / M | separate future investigation |

Effort is relative (Small/Medium/Large), not elapsed time. No classification asserts profitability.
