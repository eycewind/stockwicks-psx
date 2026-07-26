# Recommended Post-C4 Roadmap

## 1. StockIntel collector and raw capture

Implement before activating the 30-day trial. C5 must initially preserve raw
vendor frames without assuming normalized L0/L1/L2 schemas or universal vendor
sequence semantics; those remain unresolved until official samples, UAT, or
live access establish the actual contracts.

Require secret-free configuration, UAT/example validation where available,
reconnect/backoff, subscription coverage, raw preservation, server/receive
timestamps, schema versions, sizing/retention, market-hours health,
no-production-data mode, and entitlement/licensing confirmation. Preserve
vendor sequence fields when supplied and detect gaps only where the vendor
defines sequencing. Give every captured frame a locally generated capture ID
and make storage duplicate-safe even when no vendor sequence exists.
Acceptance includes recorded reconnect and applicable gap tests plus an
operational runbook.

## 2. PSX instrument, calendar, and market-data contracts

Create canonical instrument IDs, session phases/holidays/suspensions, tick/lot rules, historical/live interfaces, and normalized bar/event schemas. Adapt C3 behind the historical interface.

## 3. PSX backtest and execution model

Define next-event fills, bid/ask selection, partial fills, liquidity, price limits, fees/taxes, corporate actions, and deterministic clocks. Add chronology/leakage tests. This must precede strategy comparisons.

## 4. Canonical strategy interface and extraction

Extract one small representative strategy and the common probability decision logic. Remove data/broker/email dependencies from strategy execution. Version configurations, features, datasets, and model artifacts. Retrain on PSX data; do not port deployed US artifacts.

## 5. Signal Viewer integration

Persist `SignalIntent` records with evidence/lineage and show them against matching adjusted/raw bars. No execution side effects.

## 6. PSX scanner/ranker and Sparkie replacement

Build from the instrument master and PSX liquidity/turnover measures. Reintroduce candidate orchestration only after reliable backtest contracts; replace dollar goals and US social/Barchart lists.

## 7. Paper trading and portfolio/risk

Use live normalized quotes and the shared execution model. Add cash/exposure, lots, fees, price bands, short eligibility, reconciliation, idempotency, and kill-switch tests.

## 8. Broker integration

Implement only behind the approved broker port after paper acceptance. Require sandbox/certification, reconciliation, cancel/replace, partial fills, retry/idempotency, credential isolation, and operator controls.

Options and L2 strategies remain out of scope until product demand, legal/data entitlement, historical coverage, and execution realism are established.
