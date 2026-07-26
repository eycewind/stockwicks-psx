# Stockwicks-PSX — Contract C4

## Stockwicks Ecosystem Audit and PSX Product Architecture

### Status

Ready for implementation

### Purpose

Build a verified architectural map of the existing Stockwicks application and determine which parts can form a PSX-native research, replay, signal, paper-trading, and eventually live-trading product.

C4 is an investigation and design contract. It must not port strategies, tune parameters, add StockIntel connectivity, or modify trading behaviour.

The output must be based on code, schemas, configuration, runtime entry points, imports, and stored artifacts. Names such as `AI`, `MM`, `Sparkie`, `runner`, and `backtest` are not evidence of what a component actually does.

---

## Context

Completed work already provides:

- PSX historical daily OHLCV integration.
- A working Replay path for PSX daily bars.
- A separate Signal Viewer capable of displaying generated buy/sell markers.
- Potential access to Capital Stake/StockIntel live WebSocket feeds:
  - L0 market snapshots.
  - L1 full snapshots/top-of-book.
  - L2 depth, orders, executions, and related events.

Existing Stockwicks replay experiments on PSX symbols such as DGKC and AGP either produced no trades or large losses. C4 must explain relevant assumptions and dependencies; it must not attempt to improve profitability.

The StockIntel 30-day trial must not be activated as part of C4. A later contract will implement and validate the collector before production trial time is consumed.

---

## Objectives

1. Map Stockwicks from user-facing feature to runtime component, data source, database state, strategy logic, and external side effect.
2. Identify the canonical strategy and bot families, including Sparkie.
3. Distinguish real decision logic from runners, wrappers, experiments, duplicates, backtests, and obsolete files.
4. Determine whether any component actually uses ML and identify its model, features, training assumptions, and inference path.
5. Inventory all market-data requirements, including OHLCV, intraday bars, trades, L0, L1, L2, account state, orders, fills, and positions.
6. Identify US-market, USD, Schwab, calendar, execution, and hardcoded numeric assumptions.
7. Classify every material capability for PSX reuse, adaptation, redesign, deferral, or rejection.
8. Propose a smaller PSX-native target architecture that integrates the existing PSX database, Replay, Signal Viewer, and a future StockIntel feed collector.
9. Produce a prioritized, evidence-backed contract roadmap after C4.

---

## Non-goals

C4 must not:

- Implement or modify the StockIntel WebSocket collector.
- Start the StockIntel trial or connect using production credentials.
- Change algorithm parameters or hardcoded thresholds.
- Claim that a strategy is profitable or suitable based only on existing backtests.
- Port, refactor, or extract strategies.
- Change Replay, paper-trading, live-trading, order, or broker behaviour.
- Place orders or contact brokers.
- run a broad parameter sweep or optimization.
- Add Signal Viewer integration.
- Redesign database schemas in code.
- Commit, push, merge, or create a pull request.

Small read-only diagnostic executions are permitted. Documentation and audit artifacts required by this contract may be created or updated.

---

## Safety constraints

- All broker calls, order placement, email, notifications, and external side effects must remain disabled.
- Do not print, copy, store, or commit credentials, tokens, cookies, account identifiers, or private user data.
- Do not infer behaviour from filenames.
- Do not treat order/fill tables as exchange order-book data without proving their source.
- Do not run historical scripts that can place orders or mutate production-like state.
- Prefer static tracing and isolated read-only inspection.
- If a safe inspection requires executing a component, first prove how side effects are disabled and record that proof.
- Preserve all unrelated working-tree changes.

---

## Work packages

### T1 — Repository and product surface inventory

Inventory the application at a useful architectural level:

- web pages, APIs, commands, schedulers, workers, subprocesses, and background services;
- Replay, scanners, rankers, reports, alerts, paper trading, live trading, and administrative tools;
- named bots and assistants, especially Sparkie;
- stock, options, market-making, and other trading domains;
- configuration and feature flags controlling each capability.

For every material user-facing feature, trace:

```text
User/API entry point
  -> route/command
  -> service/runner/worker
  -> strategy or decision logic
  -> data dependencies
  -> state written
  -> external side effects
```

Exclude generic framework internals unless they materially affect this flow.

### T2 — Strategy and bot catalogue

Create a canonical strategy catalogue. For each strategy/bot family:

- canonical name and implementation;
- aliases, wrappers, copies, experiments, and obsolete variants;
- runtime entry points;
- decision rules;
- required indicators/features;
- required timeframe and warm-up;
- entry and exit behaviour;
- position-state handling;
- sizing and risk rules;
- market-data requirements;
- account/broker dependencies;
- hardcoded assumptions;
- output or order path;
- test/backtest coverage;
- evidence references.

At minimum, resolve:

- Algo1 family;
- Algo2 family;
- Algo3 family;
- Algo4 family;
- Algo5 family;
- AlgoMM and AlgoMM2;
- SMI, MACD, and RSI-named implementations;
- rankers and symbol-selection logic;
- Sparkie and any other named trading bots;
- stock versus options implementations.

Determine from code evidence what `MM` means. Do not assume it means market making.

### T3 — ML/AI verification

Search for and trace:

- model files and serialized artifacts;
- model-loading calls;
- inference frameworks;
- feature engineering;
- scalers/encoders;
- training scripts and datasets;
- target labels;
- inference outputs used in trade decisions.

For every `AI`-named component, classify it as:

- verified trained-model inference;
- deterministic indicator/rule logic;
- LLM/API-assisted logic;
- unused or incomplete;
- unresolved, with the exact missing evidence.

If trained models exist, document:

- artifact path and format;
- input features and units;
- expected symbol universe and timeframe;
- training-period evidence, if present;
- currency-sensitive and price-scale-sensitive features;
- leakage risks visible from code;
- whether retraining for PSX would be mandatory.

Do not reverse-engineer proprietary remote services or expose secrets.

### T4 — Data and database architecture

Inventory relevant database tables, caches, files, queues, and external feeds. For each, document:

- owner/writer;
- readers;
- purpose;
- key fields;
- timestamp semantics and timezone;
- retention;
- whether it represents market data, derived state, or application activity.

Explicitly distinguish:

1. Daily/intraday OHLCV.
2. Individual trades/ticks.
3. L0 snapshots.
4. L1/top-of-book quotes.
5. L2 depth/order events/executions.
6. Stockwicks-generated orders.
7. Broker executions/fills.
8. Positions, balances, and strategy state.
9. Derived indicators, scores, or rankings.

For each strategy and bot, identify its minimum required data level and whether existing PSX daily OHLCV is sufficient.

### T5 — Runtime and lifecycle architecture

Map:

- web service;
- Redis or other queues/caches;
- workers;
- schedulers;
- subprocesses/orchestrators;
- Replay lifecycle;
- paper-trading lifecycle;
- live-trading lifecycle;
- strategy runner lifecycle;
- bot start/stop and recovery behaviour;
- state persistence and heartbeat/watchdog behaviour;
- logging, metrics, and failure reporting.

Record which paths are actually active and which appear dead, duplicated, or experimental.

### T6 — External integration and side-effect map

Trace all integrations that can read or write external state:

- Schwab and any other brokers;
- market-data providers;
- email, messaging, or notification systems;
- authentication/token refresh;
- account and portfolio APIs;
- order submission, cancellation, replacement, and fill handling.

For each integration, identify the abstraction boundary, if any, and the work needed to replace it for PSX.

### T7 — Assumption audit

Find and classify assumptions including:

- US exchange calendars and trading hours;
- timezones and daylight-saving behaviour;
- USD-denominated amounts;
- absolute price thresholds and step sizes;
- tick sizes and rounding;
- fractional shares;
- pre-market and after-hours trading;
- short selling and options;
- settlement and buying-power rules;
- symbol formats and exchange identifiers;
- liquidity and volume filters;
- bid/ask spread assumptions;
- latency and fill assumptions;
- intraday-only logic;
- same-bar execution or look-ahead risks;
- missing/suspended sessions;
- corporate actions and adjusted prices.

For hardcoded numeric values, record:

- exact value;
- location;
- meaning inferred from surrounding logic;
- whether it is unitless, price-scale-sensitive, currency-sensitive, timeframe-sensitive, or market-structure-sensitive;
- recommended PSX treatment: retain, configure, normalize, recalibrate, or remove.

Do not convert USD constants to PKR mechanically. Recommend percentage, basis-point, ATR, volatility, or configuration-based alternatives where appropriate.

### T8 — PSX disposition matrix

Classify every material feature or component as one of:

- **Reuse directly**
- **Extract and adapt**
- **Redesign for PSX**
- **Defer pending intraday/L1/L2 history**
- **Reject as irrelevant, unsafe, obsolete, or unmaintainable**
- **Unresolved**

Each classification must include:

- evidence;
- required input data;
- coupling and risk;
- expected product value;
- estimated effort as Small/Medium/Large, not calendar time;
- dependencies;
- recommended next action.

### T9 — Target PSX product architecture

Propose a clean target architecture covering:

- existing PSX historical database;
- future StockIntel/Capital Stake WebSocket collector;
- immutable raw-feed capture;
- normalized L0/L1/L2 storage;
- sequence-gap and reconnect handling;
- historical and live market-data interfaces;
- independent strategy interface;
- scanner/ranker layer;
- realistic PSX backtest engine;
- Replay;
- Signal Viewer;
- paper-trading execution;
- future broker adapter;
- portfolio/risk service;
- audit logs and observability.

Define boundaries and data contracts, but do not implement them.

The design must avoid making strategies responsible for:

- fetching market data;
- directly accessing brokers;
- placing orders;
- sending notifications;
- reading web-session state;
- managing account credentials.

### T10 — Roadmap

Propose the smallest useful post-C4 contracts. At minimum consider:

1. StockIntel L0/L1/L2 collector and raw capture.
2. Canonical strategy interface and extraction.
3. PSX backtest/execution model.
4. Signal Viewer integration.
5. Paper trading.
6. Portfolio and risk management.
7. Broker integration.

Prioritize based on dependencies and ability to validate safely.

The StockIntel collector contract must be scheduled before activating the 30-day trial and must require:

- authentication without committed secrets;
- UAT/example validation where available;
- reconnect/backoff;
- subscriptions and symbol coverage;
- raw message preservation;
- server/receive timestamps;
- sequence-gap detection;
- idempotent writes;
- schema versioning;
- storage sizing and retention estimates;
- market-hours operation;
- health monitoring;
- a no-production-data test mode;
- confirmation of trial entitlement and data-retention/licensing terms.

---

## Required deliverables

Create an audit directory under the repository documentation area, following existing repository conventions. If no convention exists, use:

```text
docs/c4_stockwicks_audit/
```

Required files:

```text
00_EXECUTIVE_SUMMARY.md
01_PRODUCT_SURFACE.md
02_STRATEGY_CATALOGUE.md
03_ML_AI_AUDIT.md
04_DATA_AND_DATABASE_MAP.md
05_RUNTIME_ARCHITECTURE.md
06_EXTERNAL_INTEGRATIONS.md
07_ASSUMPTION_REGISTER.md
08_PSX_DISPOSITION_MATRIX.md
09_TARGET_PSX_ARCHITECTURE.md
10_RECOMMENDED_ROADMAP.md
EVIDENCE_INDEX.md
```

Also update the project’s normal `DELIVERY.md` with:

- C4 scope;
- files produced;
- inspection commands used;
- limitations and unresolved items;
- acceptance evidence;
- confirmation that no trading behaviour or external integration was changed.

Do not replace existing delivery history. Add C4 using the repository’s established format.

---

## Evidence standard

Every material claim must cite one or more of:

- repository-relative file path and symbol/class/function;
- database table and column;
- configuration key;
- route/command/worker entry point;
- model artifact;
- safe diagnostic output;
- test that proves the behaviour.

Use line numbers only as optional navigation aids; they are not stable identifiers.

The evidence index must map each conclusion to its supporting artifacts. Clearly label:

- verified fact;
- strong inference;
- weak inference;
- unresolved question.

Do not copy secrets or large code blocks into documentation.

---

## Acceptance criteria

C4 is accepted only when all of the following are true:

1. The major Stockwicks product surfaces and their runtime/data paths are mapped.
2. Sparkie is traced to actual implementation and dependencies, or documented as unresolved with exhaustive search evidence.
3. Canonical strategy families are separated from wrappers, duplicates, backtests, and experiments.
4. `MM` is defined from evidence or explicitly remains unresolved.
5. Every `AI`-named material component is classified based on actual model/inference evidence.
6. Relevant database tables are classified correctly, with Stockwicks orders/fills separated from exchange L0/L1/L2 data.
7. Each canonical strategy/bot has a minimum-data requirement and daily-PSX compatibility assessment.
8. The runtime map covers web, worker, scheduler, Replay, paper, live, and bot lifecycles that exist in the repository.
9. Broker and external side effects have been traced and remained disabled.
10. Hardcoded, US-market, USD, timeframe, and execution assumptions are recorded with recommended PSX treatment.
11. The disposition matrix covers all material capabilities and includes evidence, value, effort, risks, and dependencies.
12. The target architecture shows how historical PSX data, future StockIntel L0/L1/L2, strategies, backtesting, Replay, Signal Viewer, and execution fit together.
13. The roadmap identifies the collector contract that must precede activation of the StockIntel trial.
14. No strategy, Replay, broker, database, or trading behaviour was changed.
15. No credentials, generated market datasets, database dumps, logs, or machine-specific paths were added.
16. Documentation links and repository-relative paths are valid.
17. `git diff --check` passes.
18. Existing configured tests still pass, or any pre-existing failures are recorded with evidence and shown not to be caused by C4 documentation changes.
19. Final `git status` and a concise change summary are reported.
20. Nothing is committed or pushed.

---

## Required final report

At completion, report:

1. The discovered product architecture in concise terms.
2. The canonical strategies and bots.
3. Whether real ML is present and where.
4. What Sparkie actually does.
5. What `MM` actually means.
6. Which components work with daily PSX OHLCV.
7. Which components require intraday, L1, or L2 data.
8. The five highest-risk US/Stockwicks assumptions.
9. The recommended PSX product boundary.
10. The proposed next contract and why it comes first.
11. Files created or changed.
12. Tests/checks run and results.
13. Unresolved questions.
14. Confirmation that nothing was committed or pushed.

---

## Instruction to the implementing Codex agent

Execute C4 completely as specified. Begin by inspecting repository guidance, current git status, existing contract/delivery conventions, service definitions, database models/migrations, runtime entry points, and relevant tests.

Use targeted searches and dependency tracing rather than treating every file matching `*algo*` as a separate algorithm. Do not modify implementation code to make the audit easier. If runtime verification would risk external side effects, stop and document the unresolved point instead.

Do not begin C5 or the StockIntel collector. Do not commit or push.
