# C3 Delivery — Read-Only PSX Market-Data Integration

Date: 2026-07-26
Branch: `origin/c2-reproducible-local-runtime`
Baseline commit: `34af131`
Repository: `stockwicks-psx`

## Outcome

C3 is implemented as an in-process, read-only SQLite provider selected at the
existing Stockwicks price-history boundary. It supports adjusted PSX daily bars
only, preserves audit fields and unusual official source values, exposes
machine-testable quality summaries, and avoids Schwab token/OAuth/network access
in PSX mode.

No strategy, signal, indicator, backtest, fill, order, ingestion, source-data
repair, broker integration, or Celery Beat behavior was added. No merge is
performed as part of this delivery.

## Preflight and integration seam

Preflight recorded:

```text
branch: origin/c2-reproducible-local-runtime
commit: 34af131
initial status:
?? contacts/C3_read_only_PSX_market_data_integration/
?? pytest.ini
```

The active shared consumers are:

- `app/utils/stock/schwab_price_history.py`, which consumes Schwab-style candle
  dictionaries and returns an OHLCV DataFrame;
- `app/scripts/stock_algos/base_wiring.py`, whose runner consumes a
  datetime-indexed OHLCV DataFrame.

The chosen seam is an in-process provider selected by
`MARKET_DATA_PROVIDER`. This changes the two established shared history
boundaries while leaving their Schwab behavior unchanged when the setting is
absent/defaults to `schwab`.

A local HTTP compatibility service was rejected because no process boundary is
needed by these consumers. It would add a service, port, serialization,
healthcheck, and network failure mode while still requiring changes at the same
consumer boundaries.

## Source database discovery

All discovery used SQLite URI `mode=ro`. Observed real schema:

```text
daily_ohlc columns:
trade_date, symbol, sector, open, high, low, close, volume, ldcp,
open_missing, source, open_adj, high_adj, low_adj, close_adj,
volume_adj, adj_factor

rows: 621794
distinct (symbol, trade_date): 621794
range: 2020-01-01 .. 2026-07-10
DGKC: 1615 rows, 2020-01-01 .. 2026-07-10
OGDC: 1615 rows, 2020-01-01 .. 2026-07-10
```

Coverage:

```text
open_adj: 615995 / 621794
high_adj: 621794 / 621794
low_adj: 621794 / 621794
close_adj: 621794 / 621794
volume_adj: 621794 / 621794
adj_factor: 621794 / 621794
```

The missing adjusted opens correspond to the authoritative source
`open_missing` semantics. PSX C5/C9 history authorizes falling back to adjusted
close for these rows. C3 counts every fallback.

`EXPLAIN QUERY PLAN` reported:

```text
SEARCH daily_ohlc USING INDEX ix_ohlc_symbol_date
  (symbol=? AND trade_date>? AND trade_date<?)
```

The primary key also enforces `(trade_date, symbol)` uniqueness in the real
database. Temporary tests deliberately omit that constraint to exercise C3's
defensive duplicate detection.

## Data and compatibility semantics

| Compatibility field | Source/meaning |
| --- | --- |
| `datetime` | Midnight Asia/Karachi on `trade_date`, converted to Unix milliseconds |
| `open` | `open_adj`; `close_adj` only when `open_missing=1` and adjusted open is null |
| `high` | `high_adj` |
| `low` | `low_adj` |
| `close` | `close_adj` |
| `volume` | Source `volume_adj` from PSX C8 |

Raw OHLCV, `ldcp`, `adj_factor`, and the missing-open flag remain available in
typed audit records and are never mixed into adjusted candle calculations.

Supported compatibility input is `frequencyType=daily`, `frequency=1`, symbol,
and inclusive millisecond `startDate`/`endDate`. Extended hours,
`needPreviousClose=true`, period-only requests, and every non-daily frequency
fail explicitly. Known symbols with no rows in a valid range return an empty
response; unknown symbols raise a distinct error.

Quality output contains symbol, requested bounds, returned count, all
classification counts, and at most five affected-date samples per class.
Structurally invalid bars fail. Open/close-outside-range bars are preserved and
reported.

## Real DGKC/OGDC verification

Command:

```bash
python -m scripts.verify_psx_market_data \
  --db /path/to/psx_watcher.db \
  --start 2023-02-23 \
  --end 2023-03-03 \
  --symbols DGKC OGDC
```

Observed:

```text
DGKC: 7 bars, 2023-02-23 .. 2023-03-03
  open_outside_range: 1
  affected date: 2023-02-27
OGDC: 7 bars, 2023-02-23 .. 2023-03-03
  all quality counts: 0
```

The DGKC bar was preserved exactly in the compatibility result:

```text
2023-02-27
open=40.25 high=41.10 low=40.30 close=40.59 volume=625810.0
```

Source fingerprint before and after:

```text
size: 304885760
mtime_ns: 1785003631392367000
sha256: e35f224284481ab00650d6f65e495f79318f7580f340ebd6bf23fd3f08aeb67b
fingerprint_unchanged: True
```

The runner-facing `get_schwab_history(..., "1d")` path was also exercised with
requests patched to fail on any network call:

```text
DGKC 7 2026-07-02T00:00:00+05:00 2026-07-10T00:00:00+05:00 Asia/Karachi
OGDC 7 2026-07-02T00:00:00+05:00 2026-07-10T00:00:00+05:00 Asia/Karachi
```

## Live-container verification

The running development stack was inspected and the web image was rebuilt with
the final C3 and Replay code. Both default services were healthy:

```text
postgres  Up (healthy)
web       Up (healthy), 127.0.0.1:8101->8101
```

Initial inspection found the ignored local `.env.dev` pointed
`PSX_DB_HOST_PATH` at the containing directory rather than the database file.
That produced a read-only directory at `/market-data/psx.db`. The local-only
setting was corrected to the actual file and the web container was recreated.
The final Docker mount inspection reported:

```text
... -> /market-data/psx.db rw=false
```

Inside the running web container:

```text
MARKET_DATA_PROVIDER=psx_sqlite
PSX_DB_PATH=/market-data/psx.db
SCHWAB_CLIENT_ID blank: true

DGKC: 7 bars, 2026-07-02 .. 2026-07-10, Asia/Karachi
OGDC: 7 bars, 2026-07-02 .. 2026-07-10, Asia/Karachi
columns: open, high, low, close, volume
adjusted_ohlcv_match: true for both symbols
network_guard_passed: true
```

The runner-facing calls used interval `1d`. Requests and Schwab token lookup
were replaced with fail-fast guards; neither guard fired. Every returned value
was compared to `open_adj`, `high_adj`, `low_adj`, `close_adj`, and
`volume_adj` read from the mounted database.

The harmless write probe attempted to create a uniquely named table through a
SQLite URI opened with `mode=ro` and `PRAGMA query_only=ON`:

```text
write_failed: true
error: attempt to write a readonly database
```

The real database fingerprint matched the pre-verification baseline afterward:

```text
size: 304885760
mtime_ns: 1785003631392367000
sha256: e35f224284481ab00650d6f65e495f79318f7580f340ebd6bf23fd3f08aeb67b
matches_pre_verification_baseline: true
```

Commands used, with machine-specific host values replaced by placeholders:

```bash
docker compose --env-file .env.dev -f compose.dev.yml config --quiet
docker compose --env-file .env.dev -f compose.dev.yml up -d --build web
docker compose --env-file .env.dev -f compose.dev.yml ps
docker inspect stockwicks-c2-dev-web-1 --format '{{range .Mounts}}{{println .Destination "rw=" .RW}}{{end}}'
docker compose --env-file .env.dev -f compose.dev.yml exec -T web python -c '<provider/network-guard comparison>'
docker compose --env-file .env.dev -f compose.dev.yml exec -T web python -c '<read-only write probe>'
python -c '<size/mtime_ns/sha256 fingerprint comparison>'
```

## Replay daily-interval correction

The Replay templates already displayed `1 day` with submitted value `1d`; this
mapping is now protected by tests. The Replay request boundary now maps only
`1d`, `1day`, `1 day`, and `daily` to canonical `1d`. Canonical intraday
intervals remain unchanged and all unsupported values return a clear HTTP 400
validation error.

The first correction was applied to `app/routes/replay.py`, but the running
application does not register that router. `app/main.py` registers
`app/modules/replay/routes.py`, a second Replay route implementation whose
active start handler still called `fetch_and_save()` without `interval`.
Consequently Python applied its `1min` default and strict PSX mode raised
`PSX SQLite supports only the 1d interval`, which the route returned as 502.

The active route now performs the same boundary normalization and passes
`interval="1d"` explicitly by keyword on both normal and forced ingestion.
Daily Replay writes a `_1d.csv` cache with Karachi trade-date semantics, and
`ReplayDataProvider` loads that cache. The PSX runner remains strict and rejects
non-canonical aliases.

The regression test now submits an authenticated form request through the real
FastAPI application at `POST /replay-simulator/start`. It overrides only
external persistence/queue effects and asserts that the active route's real
ingestion call boundary receives exactly `1d`, creates `DGKC_1d.csv`, and
constructs the provider with canonical `1d`.

The rebuilt container exercised the complete data portion of the flow using a
temporary cache:

```text
aliases: 1d/1day/1 day/daily -> 1d
DGKC ingest: 7 daily bars, 2026-07-02 .. 2026-07-10
cache: DGKC_1d.csv
source_mode: psx_daily_v1
ReplayDataProvider: interval=1d, bars=7
replay_daily_flow_passed: true
```

The rebuilt web service was then exercised with an authenticated real POST.
The first request proved ingestion and provider loading but remained blocked in
Celery Redis-backend retries because the default Compose profile intentionally
runs only PostgreSQL and web. `compose.dev.yml` does include Redis and a worker
behind its documented explicit `worker` profile. After starting that profile,
the same POST completed:

```text
POST /replay-simulator/start
submitted interval: 1 day
normalized/stored interval: 1d
HTTP status: 200
response status: QUEUED
DGKC_1d.csv: exists
ReplayDataProvider: 7 bars, 2026-07-02 .. 2026-07-10
worker task: received with interval=1d
```

### Replay cross-container lifecycle correction

Subsequent UI polling exposed a separate container-lifecycle defect. The
worker spawned the Replay orchestrator and stored its worker-local PID, while
the web container probed that PID with `os.kill(pid, 0)` in a different PID
namespace. Web therefore marked an active Replay `ERROR` after its first
poll/candle, and the orchestrator stopped when it observed that terminal state.

Compose now sets `REPLAY_PID_LIVENESS_CHECK=false` for web and `true` for the
worker. Web relies on shared database state and orchestrator heartbeats instead
of probing worker-owned PIDs; worker-local cleanup retains PID checks. A
regression test asserts that web-mode stale cleanup does not query or reap
worker-owned processes.

After rebuilding both services, session 6 was submitted and continuously
polled through the same state/session endpoints used by the UI:

```text
DGKC 1d: 10 bars
current_bar_idx: 9
total_bars: 10
status: COMPLETED
error_message: null
false reaper warnings: none
```

### Replay daily date-range correction

The Replay templates previously set both date inputs to `today - 30 days`
regardless of interval, and the active start route always requested 30 history
sessions. The UI now applies interval-aware limits: intraday remains restricted
to 30 days, while `1d` allows up to 730 days. The route derives daily ingestion
depth from the inclusive selected calendar range, and daily ingestion permits
that larger depth without expanding intraday limits.

The rebuilt stack accepted a real DGKC Replay from 2026-01-01 through
2026-07-10:

```text
POST status: 200
daily ingestion depth: 191 days
ReplayDataProvider bars: 128
session status during verification: RUNNING
current_bar_idx: 48
error_message: null
```

## Verification results

Initial infrastructure gate:

```text
python -m pytest -q -s \
  tests/test_c2_reduced_runtime.py tests/test_c2_compose_contract.py
4 passed, 44 warnings
```

Final focused C2 regression outside bubblewrap:

```text
4 passed, 48 warnings
```

Full configured pytest discovery before the final consumer-path assertion:

```text
python -m pytest -q -s
17 passed, 48 warnings in 2.12s
```

Final run after the existing price-history consumer assertion was added:

```text
python -m pytest -q -s
18 passed, 48 warnings in 2.20s
```

Final post-runtime/Replay verification:

```text
focused C2:
4 passed, 48 warnings in 1.40s

all C3 tests:
36 passed

Replay interval tests:
22 passed

full configured pytest discovery:
40 passed, 50 warnings in 2.05s

docker compose --env-file .env.dev -f compose.dev.yml config --quiet
PASS

git diff --check
PASS
```

Other checks:

```text
python -m pip check
No broken requirements found.

python -m compileall -q app/market_data scripts/verify_psx_market_data.py
PASS

PSX_DB_HOST_PATH=/path/to/psx_watcher.db \
  docker compose --env-file env.dev.example -f compose.dev.yml config --quiet
PASS

docker compose ... config --services
postgres
web

git diff --check
PASS
```

The inherited FastAPI `TestClient` hangs when run inside the newly restored
bubblewrap sandbox, in AnyIO's blocking portal before application startup
completes. The same exact focused test passes outside bubblewrap. No application
or test accommodation was made for this tool-environment behavior.

## Complete C3 file inventory

Added:

- `pytest.ini`
- `app/market_data/__init__.py`
- `app/market_data/errors.py`
- `app/market_data/factory.py`
- `app/market_data/history.py`
- `app/market_data/models.py`
- `app/market_data/psx_sqlite.py`
- `scripts/verify_psx_market_data.py`
- `tests/test_c3_psx_market_data.py`
- `tests/test_c3_compose_contract.py`
- `tests/test_c3_replay_interval.py`
- `contacts/C3_read_only_PSX_market_data_integration/DELIVERY.md`

Modified:

- `app/config.py`
- `app/utils/stock/schwab_price_history.py`
- `app/scripts/stock_algos/base_wiring.py`
- `app/modules/replay/routes.py`
- `app/routes/replay.py`
- `app/scripts/replay/data_ingest.py`
- `app/scripts/replay/replay_data_provider.py`
- `app/services/replay_process.py`
- `app/templates/replay.html`
- `app/templates/td_replay.html`
- `compose.dev.yml`
- `env.dev.example`
- `LOCAL_DEVELOPMENT.md`
- `tests/test_c2_compose_contract.py` (replaced the now-obsolete C2 assertion
  that prohibited PSX wiring with a no-machine-specific-path assertion)

The user-supplied
`contacts/C3_read_only_PSX_market_data_integration/CONTRACT.md` was preserved.
No `app/scripts` file was changed to accommodate pytest discovery;
`base_wiring.py` was changed only because it is a verified production consumer.

## Deviations and unresolved debt

1. The integration uses an in-process provider instead of the C1-proposed HTTP
   service. This is an allowed C3 seam and is smaller for the verified
   consumers.
2. The PSX C8 contract text originally described adjusted volume in the wrong
   direction; its accepted delivery and stored `volume_adj` are authoritative.
   C3 reads the stored field without recomputation.
3. The runner initially scanned only `1.8 × lookback` calendar days. With the
   real database ending on 2026-07-10, a seven-session request on 2026-07-26
   returned empty. PSX mode now performs a bounded wider lookup and then
   re-queries exactly the requested stored-session range so quality counts match
   returned bars.
4. The first live mount inspection found the ignored local `.env.dev` selected
   the PSX data directory instead of the database file. The local setting was
   corrected, the web container rebuilt, and the final file mount/provider/write
   checks passed. No machine-specific path was added to tracked files.
5. The real database ends on 2026-07-10. C3 reports stored data faithfully and
   does not define freshness or ingestion policy.
6. Existing Pydantic/FastAPI/passlib deprecations remain inherited debt.

## Acceptance checklist

### Architecture and configuration

- **PASS** — one explicit provider boundary and factory exist.
- **PASS** — the seam is justified from actual consumer behavior.
- **PASS** — absent/default `schwab` preserves legacy selection.
- **PASS** — PSX mode tests and real smoke require no Schwab authentication.
- **PASS** — missing/invalid provider, path, SQLite, schema, and price mode fail.
- **PASS** — no real machine path is committed.

### Read-only and safety

- **PASS** — SQLite uses URI `mode=ro`, autocommit/no implicit write
  transaction, and `PRAGMA query_only=ON`.
- **PASS** — Compose renders a `:ro` host bind at a distinct container path.
- **PASS** — the running container mounts the actual database file with
  `rw=false`.
- **PASS** — an automated temporary-database write attempt fails read-only.
- **PASS** — the live-container write attempt fails read-only.
- **PASS** — real source fingerprint is identical before/after smoke.
- **PASS** — no database is copied into the repository or image.
- **PASS** — consumer tests fail on any token/network access in PSX mode.
- **PASS** — C2 safety flags and blank Schwab credentials remain asserted.
- **PASS** — Redis/worker remain optional; Beat remains absent.

### Data behavior

- **PASS** — only daily/1 is accepted.
- **PASS** — adjusted OHLC is the default and only C3 price mode.
- **PASS** — stored `volume_adj` use is evidence-based and documented.
- **PASS** — raw/audit fields remain separate.
- **PASS** — ordering and inclusive bounds are tested.
- **PASS** — timestamps are Karachi-based and host-timezone independent.
- **PASS** — duplicates and unusable rows fail.
- **PASS** — open/close-outside-range rows are preserved and classified.
- **PASS** — no source row is clipped, rewritten, or discarded.
- **PASS** — summaries are structured, bounded, and tested.

### Compatibility and verification

- **PASS** — candle fields match the verified consumer subset.
- **PASS** — unsupported interval/parameter combinations fail explicitly.
- **PASS** — temporary tests cover provider, quality, safety, compatibility,
  configuration, and Compose behavior.
- **PASS** — real DGKC `1d` compatibility and runner paths succeed.
- **PASS** — real OGDC `1d` compatibility and runner paths succeed.
- **PASS** — DGKC 2023-02-27 is preserved and classified.
- **PASS** — inherited C2 application health smoke passes outside bubblewrap.
- **PASS** — documentation contains reproducible setup and verification.
- **PASS** — full configured test discovery passes.
- **PASS** — unrelated user-owned changes were preserved and nothing staged.
- **PASS** — live Compose mount, provider, network guard, write rejection, and
  before/after fingerprint checks pass.

C3 implementation and live-container verification are complete.
