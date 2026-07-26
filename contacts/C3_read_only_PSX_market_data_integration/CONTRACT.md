# Contract C3: Read-Only PSX Market-Data Integration

## 1. Objective

Integrate the inherited Stockwicks application with historical Pakistan Stock
Exchange (PSX) daily market data from the existing `psx-stock-watcher` SQLite
database.

C3 must deliver:

* a read-only SQLite market-data provider;
* the smallest compatibility layer required by Stockwicks' existing
  price-history consumer;
* explicit raw-versus-adjusted field semantics;
* non-destructive data-quality reporting;
* configuration and Docker Compose wiring for a host-side PSX database;
* automated provider, compatibility, safety, and integration tests;
* a reproducible DGKC and OGDC daily-data smoke test.

C3 proves that Stockwicks can retrieve trustworthy, chronologically ordered PSX
daily bars. It does **not** prove that an inherited Stockwicks algorithm is
suitable for daily PSX data, and it does not implement a strategy backtest,
signals, order simulation, or live trading.

## 2. Background

C1 discovered that Stockwicks obtains historical candles through a Schwab
price-history integration. The known consumer expects candle objects containing:

```text
datetime
open
high
low
close
volume
```

C2 delivered a reproducible FastAPI/PostgreSQL development runtime with:

* a `web` and `postgres` default Compose topology;
* optional Redis and Celery worker services behind an explicit profile;
* all real trading and brokerage access forced off;
* reserved `MARKET_DATA_PROVIDER` and `MARKET_DATA_BASE_URL` configuration
  variables;
* no PSX database mount or PSX provider implementation.

The source PSX database is expected outside this repository, commonly at:

```text
../psx-stock-watcher/data/psx_watcher.db
```

Its relevant `daily_ohlc` data includes:

```text
symbol
trade_date
open
high
low
close
volume
open_adj
high_adj
low_adj
close_adj
adj_factor
ldcp
```

The path and exact schema must be verified during implementation. Do not assume
that this summary replaces repository or database inspection.

The consolidated C1-C12 history from `psx-stock-watcher` establishes important
source semantics:

* official PSX values must be preserved;
* close may legitimately fall outside the recorded intraday high-low range
  under the project's established source convention;
* open outside high-low remains a quality concern, but is a warning rather than
  automatic evidence that a row must be rejected;
* adjusted prices inherit source characteristics from raw values;
* bars must never be silently clipped, expanded, rewritten, or discarded;
* indicators and returns use adjusted OHLCV-equivalent inputs;
* raw OHLCV and `ldcp` remain available for audit and circuit-limit logic;
* circuit calculations use raw `open / ldcp`, never adjusted open against raw
  `ldcp`;
* a signal calculated using day D data may execute no earlier than D+1 open.

The DGKC history contains at least one unusual OHLC bar and OGDC was clean under
the earlier diagnostic query. C3 must preserve and report such source values
rather than "repairing" them.

## 3. Governing Rules

1. Read and follow repository instructions before changing files.
2. Read the accepted C1 and C2 contracts and deliveries.
3. Read the consolidated `psx-stock-watcher` C1-C12 history and inspect relevant
   source contracts or code where the summary identifies uncertainty.
4. Preserve all unrelated modified, deleted, and untracked files.
5. Do not use `git add .`, `git add -A`, broad formatting, or unrelated cleanup.
6. Treat the PSX SQLite database as immutable.
7. Open SQLite in enforced read-only mode. A filesystem read-only mount alone is
   defense in depth, not sufficient proof.
8. Do not create tables, indexes, journals, WAL files, temporary sidecar files,
   migrations, corrections, or metadata in the source database.
9. Do not copy the production PSX database into this repository or Docker image.
10. Do not expose secrets or commit a machine-specific database path.
11. Do not call Schwab or any broker when the PSX provider is selected.
12. Keep C2 trading safety controls intact.
13. Do not silently substitute data, dates, symbols, intervals, or price scales.
14. Do not modify source OHLC values to force textbook candle invariants.
15. Do not commit or push unless separately instructed.

If a required target file contains overlapping, uncommitted user edits, stop and
report the overlap before editing it.

## 4. Scope

### T1 — Preflight and integration-boundary discovery

Before implementation:

1. Record the current branch, commit, concise Git status, and active C2 runtime
   state.
2. Identify all application paths that fetch or consume Schwab price history.
3. Trace the exact request and response shape used by the application, including:

   * symbol;
   * period and frequency fields;
   * start/end timestamps;
   * candle timestamp units and timezone assumptions;
   * empty-result behavior;
   * error behavior;
   * any `previousClose` or metadata fields actually consumed.
4. Determine whether the smallest safe seam is:

   * a provider selected inside the Stockwicks application; or
   * a small local HTTP compatibility service selected through
     `MARKET_DATA_BASE_URL`.
5. Prefer the approach that changes the fewest established consumer paths while
   keeping PSX-specific behavior isolated and directly testable.
6. Document why the chosen seam is smaller and safer than the rejected option.
7. Inspect the actual PSX schema using read-only SQLite metadata queries.
8. Verify:

   * the `daily_ohlc` column names and types;
   * uniqueness or duplication of `(symbol, trade_date)`;
   * date representation;
   * adjusted-field coverage;
   * volume semantics;
   * available date ranges for DGKC and OGDC;
   * relevant indexes and expected query plan.

Discovery must not perform writes to either application database or PSX
database.

### T2 — Provider interface and configuration

Introduce an explicit market-data provider boundary rather than scattering
PSX-specific conditionals through algorithms, routes, or Schwab authentication
code.

The configuration must support at least:

```text
MARKET_DATA_PROVIDER=schwab|psx_sqlite
PSX_DB_PATH=/absolute/container/path/to/psx_watcher.db
PSX_PRICE_MODE=adjusted
```

Requirements:

* Preserve existing behavior when `MARKET_DATA_PROVIDER` is absent or set to the
  documented legacy value, unless C2 already established a safer default.
* C3 development examples must select `psx_sqlite`.
* `PSX_DB_PATH` must have no production-machine default in committed code.
* Fail clearly at startup or first provider use if the provider name is unknown,
  the file is absent, unreadable, not a SQLite database, or lacks required
  columns.
* Reject relative container paths if their resolution would be ambiguous.
* The selected PSX path and provider may be logged, but credentials, unrelated
  environment values, and sensitive host paths must not be dumped.
* Provider selection must be dependency-injectable or otherwise replaceable in
  tests.

If the actual application already has an equivalent provider abstraction, extend
it instead of introducing a parallel one.

### T3 — Read-only SQLite provider

Implement a narrowly scoped provider, named consistently with the codebase (for
example `PsxSqliteMarketDataProvider`), which:

* opens SQLite through a read-only URI or equivalent driver-enforced mechanism;
* prevents accidental implicit transactions from writing;
* queries only the required columns;
* normalizes symbols by a documented rule;
* uses bound parameters for all query values;
* never interpolates a symbol, date, column, or sort direction into SQL without
  explicit validation;
* returns bars ordered by `trade_date` ascending;
* applies inclusive start/end date filtering with documented boundary semantics;
* detects duplicate `(symbol, trade_date)` rows in the requested range;
* gives distinct results for:

  * unknown symbol;
  * valid symbol with no rows in the requested range;
  * malformed request;
  * provider/database failure;
* does not silently drop rows.

Only daily frequency is supported in C3. Any minute, weekly, monthly, or
unsupported Schwab frequency combination must return a clear unsupported-
interval error. Do not resample daily bars or fabricate intraday candles.

The provider must not import or depend on trading, broker, order, Celery Beat, or
strategy modules.

### T4 — Price and volume semantics

The default C3 candle payload must use:

```text
open   = open_adj
high   = high_adj
low    = low_adj
close  = close_adj
volume = documented adjusted-volume value if one exists;
         otherwise the source volume, explicitly labelled/documented as such
```

Do not invent `volume_adj`. Determine from the PSX implementation history whether
volume is adjusted elsewhere and document the result.

Requirements:

* Preserve source numeric precision; avoid unnecessary rounding.
* Reject non-finite values.
* Missing adjusted OHLC in a requested row is an error unless an explicitly
  documented project rule authorizes a fallback.
* The previously established missing-open fallback (`open := close`) may be used
  only if the source contracts establish it as authoritative for the relevant
  field/mode. Every fallback occurrence must be counted and reported.
* Never infer high or low from open/close.
* Never alter raw or adjusted values in the database.
* Do not mix adjusted OHLC with raw `ldcp` for return, signal, or circuit
  calculations.

Raw OHLC, `adj_factor`, and `ldcp` need not be added to the inherited Schwab
candle shape if consumers do not accept them. They must remain accessible to
provider validation/audit code or a separate internal typed record so future
contracts can perform scale-correct checks.

### T5 — Data-quality classification and reporting

For every requested range, classify at least:

* missing required adjusted field;
* duplicate date;
* non-positive OHLC;
* non-finite OHLCV;
* negative volume;
* `high < low`;
* `open` outside `low..high`;
* `close` outside `low..high`;
* missing-open fallback, if applicable;
* non-positive or missing `adj_factor`, where relevant;
* missing or invalid `ldcp`, where relevant to audit data.

Policy:

| Condition                                 | C3 behavior                                                    |
| ----------------------------------------- | -------------------------------------------------------------- |
| Duplicate `(symbol, trade_date)`          | Fail the request                                               |
| Missing/non-finite required adjusted OHLC | Fail the request                                               |
| Non-positive adjusted OHLC                | Fail the request                                               |
| `high < low`                              | Fail the request                                               |
| Negative or non-finite volume             | Fail the request                                               |
| Open outside high-low                     | Preserve bar and emit a structured warning/count               |
| Close outside high-low                    | Preserve bar and emit a structured informational warning/count |
| Missing-open fallback authorized and used | Preserve fallback result and emit a structured warning/count   |
| Missing audit-only `ldcp`                 | Preserve candle retrieval; report audit limitation             |

If the PSX contract history explicitly defines a different rule, follow that
rule and cite it in the delivery. Do not silently change the table above.

Warnings must be machine-testable and summarized without producing one noisy log
line per row in a large range. At minimum expose:

* symbol;
* requested date range;
* returned bar count;
* count per quality classification;
* a bounded sample of affected dates.

C3 must not label close-outside-range alone as corrupt market data.

### T6 — Compatibility response

Adapt provider output to the exact existing price-history shape established in
T1. At minimum each returned candle must contain:

```json
{
  "datetime": 0,
  "open": 0,
  "high": 0,
  "low": 0,
  "close": 0,
  "volume": 0
}
```

Requirements:

* `datetime` must use the unit expected by the consumer.
* Map a PSX trade date to a documented timezone convention using
  `Asia/Karachi`; do not use the host's local timezone implicitly.
* Date-only bars must not shift to the preceding/following date when converted
  to epoch time.
* The response must be deterministic across host timezones.
* Preserve ascending chronological order.
* Preserve the exact top-level response keys actually required by Stockwicks.
* Unsupported parameters must fail explicitly rather than being ignored when
  ignoring them could change the requested dataset.
* Unknown symbols and empty date ranges must follow documented, tested response
  semantics.
* Do not claim complete Schwab API compatibility. Implement and document only
  the subset required by the verified Stockwicks consumer.

If implemented as HTTP, bind the compatibility endpoint only within the local
development topology unless the user explicitly requests broader exposure.

### T7 — C2 runtime and Compose integration

Extend the C2 development runtime without weakening it.

Requirements:

* Mount the configured host PSX database into the consuming container as
  read-only.
* Use a container path distinct from the host path.
* Do not bake the database into an image.
* Do not commit `.env.dev` or a real host path.
* Add sanitized configuration examples and documentation.
* Preserve the default C2 service set unless the chosen architecture genuinely
  requires a separate compatibility service.
* If a new service is required:

  * justify it in the delivery;
  * give it a healthcheck;
  * expose no unnecessary host port;
  * preserve the default absence of Redis, worker, and Beat;
  * keep PostgreSQL and Redis unpublished.
* Continue forcing all C2 trading safety variables to their safe values.
* Starting with `MARKET_DATA_PROVIDER=psx_sqlite` must require no Schwab token,
  OAuth flow, or network request.
* Restarting the stack must not alter the source database.

Document both a Linux path example and the fact that Docker Desktop/WSL users
must provide a path visible to the WSL/Docker environment. Do not hard-code
`/home/hassan/...`.

### T8 — Automated tests

Tests must use temporary SQLite databases constructed by test fixtures. They
must not depend on or mutate the user's production PSX database.

Provider tests must cover:

* valid adjusted daily bars;
* ascending order;
* inclusive date boundaries;
* symbol normalization and SQL-injection-like symbol input;
* unknown symbol;
* valid symbol with an empty requested range;
* missing database;
* invalid schema;
* enforced read-only behavior;
* duplicate dates;
* missing adjusted fields;
* non-finite/non-positive OHLC;
* `high < low`;
* open outside range preserved and warned;
* close outside range preserved and reported, not rejected;
* negative volume;
* deterministic timestamp conversion under at least two host timezone settings;
* unsupported intervals/frequencies;
* absence or authorized behavior of missing open.

Compatibility/integration tests must cover:

* exact consumer-required response shape;
* no Schwab/OAuth/broker network request in PSX mode;
* C2 safety variables remain effective;
* Compose mounts the database read-only;
* no Beat service/process;
* no public PostgreSQL/Redis port;
* application health and at least one real consumer path using the PSX provider.

Include a source-database immutability check around the real read-only smoke
test. Use a safe fingerprint such as file size, modification timestamp, and
cryptographic hash before and after. A changed fingerprint fails acceptance.

### T9 — Real-data smoke verification

Using the configured real database in read-only mode:

1. Retrieve DGKC daily bars for a bounded, documented date range containing
   multiple sessions.
2. Retrieve OGDC daily bars for the same or a comparable bounded range.
3. Verify:

   * non-empty results;
   * ascending dates;
   * requested boundaries;
   * adjusted field mapping;
   * deterministic timestamps;
   * quality summary;
   * no source-database change.
4. Include a DGKC range containing its known unusual bar if its date can be
   established safely. Prove the bar is preserved and classified according to
   the agreed semantics.
5. Exercise the Stockwicks-facing compatibility path, not only a direct SQL
   helper.

Do not run or assess an inherited trading algorithm under C3. A "daily data
loaded successfully" result is not a strategy result.

### T10 — Documentation

Create or update development documentation with:

* architecture and provider-selection flow;
* supported compatibility subset;
* required environment variables;
* safe database-path setup;
* Compose startup/restart/shutdown commands;
* direct provider or endpoint smoke commands;
* DGKC and OGDC verification commands;
* raw versus adjusted field mapping;
* timestamp and timezone semantics;
* error and quality-warning policy;
* limitations and deferred work;
* instructions proving the source DB is mounted and opened read-only.

Do not paste secrets, full local environment files, or machine-specific absolute
paths into committed documentation.

## 5. Explicitly Out of Scope

C3 must not:

* implement, tune, or evaluate trading strategies;
* adapt intraday algorithms to daily bars;
* generate buy/sell signals;
* execute a backtest or simulate fills;
* implement D+1 orders beyond preserving/documenting the rule for future work;
* implement circuit-limit trading logic;
* rank a tradeable universe or relative strength;
* calculate indicators unless strictly required to prove a pre-existing consumer
  can receive candles;
* add minute or tick PSX data;
* resample daily data;
* ingest or backfill PSX data;
* fix source OHLC rows;
* perform corporate-action recalculation;
* write to `psx_watcher.db`;
* add broker connectivity, order placement, OAuth, or live/paper trading;
* enable Celery Beat;
* redesign unrelated Stockwicks modules;
* claim profitability, strategy validity, or production readiness.

Algorithm execution against DGKC daily data belongs to a later contract after
the data seam is verified and the algorithm's timing and fill assumptions are
made daily-compatible.

## 6. Required Deliverables

1. C3 implementation files.
2. Provider and integration tests.
3. Updated sanitized environment example.
4. Updated Compose/runtime wiring.
5. Updated local-development/integration documentation.
6. `DELIVERY.md` containing:

   * outcome and implementation summary;
   * chosen integration seam and rejected alternative;
   * complete added/modified file inventory;
   * exact commands executed;
   * exact test and smoke-test results;
   * real DGKC/OGDC ranges and bar counts;
   * raw/adjusted/volume/timestamp mappings;
   * data-quality counts and bounded affected-date samples;
   * source DB before/after fingerprints;
   * contract deviations;
   * unresolved issues and inherited debt;
   * final concise Git status;
   * an acceptance checklist with PASS/FAIL for every criterion.

Delivery claims are not evidence unless supported by test output, inspected
runtime state, or reproducible commands.

## 7. Acceptance Criteria

C3 is accepted only when all applicable criteria pass.

### Architecture and configuration

* [ ] One explicit market-data provider boundary exists.
* [ ] The chosen seam is justified from actual consumer behavior.
* [ ] Legacy provider behavior is preserved outside PSX mode.
* [ ] PSX mode requires no Schwab authentication.
* [ ] Missing/invalid provider configuration fails clearly.
* [ ] No real PSX database path is committed.

### Read-only and safety

* [ ] SQLite is opened with driver-enforced read-only semantics.
* [ ] Compose mounts the database read-only.
* [ ] A write attempt fails in an automated test.
* [ ] The real source database fingerprint is unchanged after smoke tests.
* [ ] No database copy is baked into an image or committed.
* [ ] No Schwab, broker, or OAuth network request occurs in PSX mode.
* [ ] All C2 trading safety controls remain effective.
* [ ] Redis/worker remain optional and Beat remains absent.

### Data behavior

* [ ] Only daily bars are accepted.
* [ ] Returned bars use adjusted OHLC by default.
* [ ] Volume behavior is evidence-based and documented.
* [ ] Raw/audit fields remain distinguishable from adjusted candle fields.
* [ ] Bars are ordered ascending with inclusive documented boundaries.
* [ ] Timestamp conversion is Asia/Karachi-based and host-timezone independent.
* [ ] Duplicate and structurally unusable rows fail loudly.
* [ ] Open/close-outside-range values are preserved and classified.
* [ ] No row is silently clipped, rewritten, or removed.
* [ ] Quality summaries are structured, bounded, and machine-testable.

### Compatibility and verification

* [ ] The response matches the exact subset consumed by Stockwicks.
* [ ] Unsupported interval/parameter combinations fail explicitly.
* [ ] Temporary-database tests cover all cases required by T8.
* [ ] A real Stockwicks-facing DGKC `1d` request succeeds.
* [ ] A real Stockwicks-facing OGDC `1d` request succeeds.
* [ ] The known DGKC unusual bar is preserved and correctly classified when its
  date is available.
* [ ] Application health and existing C2 smoke checks still pass.
* [ ] Documentation gives reproducible setup and verification commands.
* [ ] Full relevant tests pass.
* [ ] No unrelated user-owned change is modified or staged.

Any failed criterion must remain marked FAIL in `DELIVERY.md`; do not describe C3
as complete while a mandatory criterion remains unverified.

## 8. Recommended Implementation Sequence

1. Complete preflight and map the exact price-history consumer.
2. Inspect the SQLite schema and PSX contract history.
3. Define typed provider records and errors.
4. Implement the read-only SQLite provider.
5. Implement quality classification.
6. Add the smallest consumer compatibility adapter.
7. Wire configuration and Compose read-only mounting.
8. Add temporary-database tests.
9. Run existing C2 regression tests.
10. Run bounded DGKC/OGDC real-data smoke tests with immutability checks.
11. Update documentation and `DELIVERY.md`.

Do not start by changing algorithms or by reproducing the entire Schwab API.

## 9. Executioner Handoff Prompt

```text
Implement Contract C3: Read-Only PSX Market-Data Integration.

Read the full C3 contract, accepted C1/C2 contracts and deliveries, repository
instructions, and the consolidated psx-stock-watcher C1-C12 history before
editing.

Begin with read-only preflight. Trace the actual Stockwicks price-history
consumer and select the smallest explicit provider seam. Inspect the real PSX
SQLite schema in read-only mode.

Implement only daily PSX candle retrieval and the exact compatibility subset the
verified consumer needs. Use adjusted OHLC by default, preserve raw values for
audit, keep timestamps deterministic in Asia/Karachi, and never repair or
discard open/close-outside-range bars. Open and mount the source database
read-only.

Use temporary SQLite fixtures for automated tests. Then run bounded real-data
smoke tests for DGKC and OGDC through the Stockwicks-facing path, proving the
source database fingerprint is unchanged. Keep all C2 trading and network safety
controls intact.

Do not run algorithms, generate signals, backtest, add broker functionality,
enable Beat, modify source PSX data, or expand into later-contract work.

Preserve all unrelated worktree changes. Do not commit or push unless separately
instructed. Produce DELIVERY.md with the complete evidence and acceptance
checklist required by C3.
```
