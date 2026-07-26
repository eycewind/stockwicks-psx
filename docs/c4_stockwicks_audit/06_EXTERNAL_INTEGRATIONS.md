# External Integrations and Side Effects

| Integration | Boundary and purpose | Reads/writes | PSX treatment |
|---|---|---|---|
| Schwab market data | `base_wiring`, quote/history utilities | OAuth token; price history/quotes | Replace with market-data interface |
| Schwab trading | broker routes, `schwab_trade.py`, paper/live services | accounts, orders, cancel/replace | Redesign behind broker adapter |
| Schwab OAuth | broker/auth routes and token tasks | authorization/token refresh; encrypted DB tokens | US-only; retain generic credential patterns |
| PSX SQLite | `app/market_data` provider | read-only daily bars | Reuse directly |
| Barchart | `barchart_symbols.py` | public/site HTTP, optional snapshots | Reject as PSX universe source |
| Stocktwits | `stocktwits_symbols.py` | trending/watchers HTTP | Reject as PSX universe source |
| Email/SMTP | `email_service.py`, paper/SPX services | sends notifications, logs dedupe | Extract notification port; disable in research |
| Local LLM | analysis scripts with Mistral GGUF path | local model inference | Unresolved/incomplete; machine-specific legacy |
| Tradier/IB catalog | broker UI placeholders | setup pages only in proved path | Defer; no proved adapter |

No StockIntel/Capital Stake connection was attempted. No repository boundary for that feed exists yet.

## Side-effect controls

Development Compose sets `TRADING_ENABLED=false`, `PAPER_TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false`, `EMERGENCY_STOP=true`, and blank external credentials. Live equity mirroring additionally requires both trading/live flags. Nonetheless, several legacy scripts call HTTP APIs directly, so scripts must never be executed merely for discovery.

## Replacement boundary

Define independent ports:

- `HistoricalMarketData` and `LiveMarketData` returning normalized events/bars.
- `BrokerAdapter` for account, order, cancel/replace, fill, position.
- `NotificationSink`.
- `CredentialStore`.

Strategies receive market snapshots and portfolio context and emit intents only; they must not obtain tokens, issue HTTP requests, send email, or mutate web-session state.
