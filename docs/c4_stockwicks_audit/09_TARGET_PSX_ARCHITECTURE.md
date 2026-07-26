# Target PSX Architecture

```text
StockIntel WebSocket -> Raw immutable capture -> Normalizer -> L0/L1/L2 event store
                                                        -> Bar builder
Existing PSX SQLite -> Historical adapter --------------> Market Data API
                                                            |
Instrument master + calendar -------------------------------+
                                                            v
Scanner/ranker -> Strategy interface -> Signal/intent store -> Signal Viewer
                                          |
                                          +-> Backtest/Replay execution model
                                          +-> Paper execution simulator
                                          +-> Portfolio/risk service
                                          +-> Future broker adapter
All components -> audit events, metrics, health, lineage
```

## Contracts

- `MarketEvent`: schema version, exchange, instrument ID, event type, source/server/receive timestamps, sequence, payload, raw-record reference.
- `Bar`: instrument, interval, session date, open/high/low/close/volume, adjustment mode, completeness, lineage.
- `StrategyContext`: ordered market inputs, portfolio snapshot, calendar state, configuration/model version.
- `SignalIntent`: strategy/version, timestamp, instrument, side, confidence, horizon, reason/features, no execution method.
- `OrderIntent`: portfolio-approved quantity/order constraints and idempotency key.
- `ExecutionEvent`: accepted/rejected/partial/filled/cancelled, price, quantity, fees, timestamps, source.

## Collector requirements

Preserve raw frames before normalization; store server and receive time; detect duplicate/out-of-order/gap sequences; reconnect with bounded exponential backoff; resubscribe idempotently; schema-version normalized records; expose lag/coverage/gap health; estimate storage and retention before trial. Confirm entitlement, redistribution, and retention terms. Provide recorded/UAT/no-production-data modes.

## Engine boundaries

Historical and live sources implement the same ordered market-data interface. Backtest, Replay, and paper trading share one explicit execution model but different clocks. Strategies are pure consumers/producers: they do not fetch data, access brokers, place orders, notify users, read web sessions, or manage credentials.

Portfolio/risk owns cash, exposure, lots, short eligibility, price bands, fees, limits, and kill switches. Broker adapters translate approved order intents and reconcile fills/positions. Signal Viewer reads immutable signal records, not strategy process memory.
