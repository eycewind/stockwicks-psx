# Contract C1: Application Discovery and Local Runtime Plan

## 1. Objective

Inspect the inherited stock-trading application and produce an evidence-based plan for running it locally and connecting it to a PSX market-data API.

This is a discovery contract. Do not implement the PSX API, refactor the application, install system services, or attempt a production deployment during C1.

## 2. Context

The application was developed for US markets and currently obtains market data from the Charles Schwab API. The complete application is expected to be available in the current working directory.

The target PSX environment has historical daily OHLCV data in the `psx-stock-watcher` SQLite database. A future contract will expose that data through a limited Schwab-compatible API so the inherited application can run against PSX data with minimal changes.

An observed probe script calls:

```http
GET https://api.schwabapi.com/marketdata/v1/pricehistory
```

It requests minute or daily candles and consumes:

```text
candles[].datetime
candles[].open
candles[].high
candles[].low
candles[].close
candles[].volume
```

The production deployment script indicates that the application may contain:

- A Python web application in `app/main.py`
- A Celery worker in `app/celery_worker.py`
- A Celery Beat scheduler
- A database layer
- Persistent model artifacts
- A `/healthz` endpoint
- Separate web, worker, and scheduler processes

These observations are leads, not confirmed architecture. Verify them from the repository.

## 3. Working Rules

1. Treat the inherited source code as read-only during discovery, except for the C1 documentation files explicitly required by this contract.
2. Do not run the supplied production deployment script.
3. Do not use `sudo`, modify `/var`, install systemd services, or restart host services.
4. Do not call the live Schwab API unless the user explicitly authorizes it.
5. Do not display, copy, commit, or record secret values.
6. Environment files may be inspected only for variable names and structure. Redact all values in documentation and terminal output.
7. Do not install dependencies or run database migrations during C1.
8. Safe static inspection, import-tree analysis, configuration discovery, and non-mutating version checks are allowed.
9. Do not assume Docker is the correct local runtime until the application’s dependencies have been identified.
10. Preserve all unrelated user changes in the working tree.

If repository instructions such as `AGENTS.md` exist, read and follow them before inspecting or changing files.

## 4. Scope

### T1 — Repository and Runtime Inventory

Identify and document:

- Top-level applications, packages, services, and frontend directories
- Python, Node.js, or other runtime versions
- Dependency manifests and lock files
- Application entry points
- Web framework and server
- Background workers and scheduled jobs
- Database engine, ORM, models, and migration system
- Message broker and cache requirements
- Frontend build/runtime requirements
- Model files and other required persistent assets
- Configuration sources and required environment-variable names
- Existing Docker, Compose, systemd, reverse-proxy, CI, and deployment files
- Health checks and default ports

Do not list secret values.

### T2 — Schwab Dependency Audit

Search the complete repository for all Schwab-related integration points, including:

```text
api.schwabapi.com
schwab
pricehistory
marketdata
quotes
instruments
market hours
Authorization
Bearer
get_valid_access_token
frequencyType
periodType
startDate
endDate
needPreviousClose
needExtendedHoursData
```

For every actual API call, record:

- Source file and function or class
- HTTP method and endpoint
- Query parameters
- Request headers
- Response fields consumed
- Expected error behavior
- Calling process: web, worker, scheduler, CLI, or test
- Whether the call is essential for daily historical PSX processing

Also identify:

- Token acquisition and refresh behavior
- Hardcoded Schwab URLs
- Configuration points that could select a different API base URL
- SDKs or wrappers that may hide direct HTTP calls
- Tests, fixtures, cached payloads, or sample Schwab responses

Separate confirmed production calls from probes, examples, dead code, and tests.

### T3 — Market and Time Assumption Audit

Identify assumptions tied specifically to US markets:

- `US/Eastern` or `America/New_York` timezone handling
- Market open and close times
- Premarket and after-hours sessions
- NYSE/Nasdaq trading calendars and US holidays
- Intraday candle requirements
- Ticker formatting and exchange suffixes
- Currency assumptions
- Previous-close behavior
- Price precision and tick-size assumptions
- Volume and liquidity assumptions
- Corporate actions and adjusted versus unadjusted OHLC
- Short selling, fractional shares, order types, and settlement assumptions
- Fees, commissions, taxes, and slippage

For each assumption, classify it as:

- Compatible with PSX as-is
- Requires configuration
- Requires code change
- Unsupported by the available daily PSX dataset
- Requires further investigation

Pay particular attention to timestamp conversion. The observed probe converts all returned timestamps to US Eastern time; a PSX trade date encoded incorrectly could move to the previous calendar day.

### T4 — Local Runtime Dependency Map

Produce a dependency map showing:

- Processes that must run
- Databases, brokers, caches, and storage they use
- Startup dependencies
- Network ports
- Required persistent volumes or directories
- External cloud services
- Optional versus mandatory components

Determine whether the portal can run in a reduced local mode for historical-data and algorithm testing. Do not assume every production service is necessary.

### T5 — Local Deployment Recommendation

Recommend the smallest safe local deployment that can:

1. Start the required application components.
2. Display or operate the relevant portal functions.
3. Fetch daily historical data through a configurable API endpoint.
4. Execute the friend’s signal-generation workflow.
5. Preserve logs and test outputs.

Compare only the viable options found during inspection, such as:

- Existing Docker/Compose configuration
- A new development-only Compose configuration
- Native virtual environment plus separately started dependencies

Select one approach and justify it using repository evidence. List prerequisites, required sanitized configuration, missing artifacts, and unresolved blockers.

### T6 — PSX Compatibility API Requirements

Define the minimum API surface required by the production application. This is a specification only; do not implement it in C1.

For each required endpoint, document:

- Method and path
- Supported parameters
- Required response schema
- Timestamp units and timezone semantics
- Empty-result behavior
- Invalid-symbol behavior
- Unsupported-frequency behavior
- Authentication behavior for local use

Explicitly distinguish:

- Daily functionality supported by the current PSX database
- Intraday functionality that cannot be supported honestly
- Optional endpoints unrelated to initial strategy evaluation

Do not silently propose daily candles as substitutes for requested intraday candles.

### T7 — Implementation Contract Plan

Propose the next contracts based on findings. At minimum, consider:

- Local development runtime
- PSX Schwab-compatible API
- Application/API integration
- Strategy execution and validation

Each proposed contract should have a short objective and clear boundary. Do not write the full later contracts during C1.

## 5. Required Deliverable

Create `DELIVERY.md` at the repository root with the following sections:

1. Executive Summary
2. Repository and Runtime Inventory
3. Process and Dependency Map
4. Schwab API Call Inventory
5. Authentication and Configuration Findings
6. US-Market Assumptions
7. Minimum PSX Compatibility API Specification
8. Recommended Local Runtime
9. Missing Inputs and Blockers
10. Risks and Open Questions
11. Proposed Next Contracts
12. Inspection Evidence

Use tables where they improve exact mappings. Include file paths and symbol names for important findings. Do not include secrets or full environment-file contents.

Under **Inspection Evidence**, list the non-mutating commands used and summarize relevant outputs. Avoid pasting large command dumps.

If an important conclusion cannot be verified, label it clearly as an inference rather than a fact.

## 6. Acceptance Criteria

C1 is complete only when:

- [ ] All application entry points and required processes are identified.
- [ ] Runtime and dependency manifests are identified.
- [ ] Database, broker, cache, frontend, and model dependencies are documented.
- [ ] Every repository-level Schwab integration point has been classified.
- [ ] Production API calls are separated from probes, tests, and dead code.
- [ ] Required request parameters and consumed response fields are documented.
- [ ] Authentication and base-URL configuration paths are understood.
- [ ] Intraday requirements are explicitly identified.
- [ ] US timezone, calendar, symbol, price, and execution assumptions are assessed.
- [ ] A minimal local runtime is recommended using repository evidence.
- [ ] The minimum PSX compatibility API is specified without implementing it.
- [ ] Missing secrets, services, databases, assets, or configuration templates are listed.
- [ ] No secret values appear in `DELIVERY.md` or command output included there.
- [ ] No production deployment, system service modification, migration, or live Schwab request was performed.
- [ ] `DELIVERY.md` contains enough evidence to create C2 without repeating discovery.

## 7. Stop Conditions

Stop and report the blocker instead of guessing if:

- Required source directories or dependency manifests are missing.
- Inspection requires exposing credentials.
- The only available database contains sensitive production data and no sanitized alternative exists.
- A required component can only be understood by running a destructive migration or production deployment.
- Repository instructions conflict with this contract.
- It is unclear which code is owned by the friend and which changes are permitted.

## 8. Out of Scope

The following are explicitly outside C1:

- Implementing the PSX API
- Modifying the strategy or signal algorithms
- Backtesting or assessing profitability
- Installing or upgrading dependencies
- Creating production infrastructure
- Running the production deployment script
- Configuring DNS, TLS, reverse proxies, or public access
- Replacing Schwab authentication
- Fabricating intraday PSX data
- Committing or pushing changes unless separately requested by the user

## 9. Completion Response

When finished, provide a concise response containing:

- Overall discovery result
- Recommended local-runtime approach
- Number of confirmed Schwab endpoints required
- Primary blockers or missing inputs
- Recommended next contract
- Link or path to `DELIVERY.md`

Do not paste the full delivery document into chat.
