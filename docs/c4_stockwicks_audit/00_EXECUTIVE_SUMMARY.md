# C4 Executive Summary

## Scope and method

C4 is a static, evidence-led audit. No broker, email, StockIntel, trading, or historical-script execution was performed. Statements are marked **Verified**, **Inference**, or **Unresolved**. Evidence is indexed in `EVIDENCE_INDEX.md`.

## Findings

- **Verified:** The active stock-bot runtime path is FastAPI → `paper_trade_bot` → Celery stock task → `stock_bot_runner` → one of Algo1–5/SMI/MACD → paper trade service, optionally Schwab live mirror. Safety flags exist, but strategy, data acquisition, execution, and notification concerns remain coupled.
- **Verified:** Algo1–5 are directional supervised-classification strategies. “MM” does not implement quoting, inventory control, spread capture, or two-sided orders; it predicts upward probability from OHLCV-derived features and opens LONG/SHORT positions. It should not be described as market making.
- **Verified:** Algo1–5 use `HistGradientBoostingClassifier` artifacts serialized with joblib. The default target is a forward return direction over `k_forward=3`; live code can train missing/stale artifacts from recent bars. This is real ML, but not evidence of profitability.
- **Verified:** Algo1/4 use a 12-feature technical set; Algo2 adds OBV/VWAP behavior; Algo3 uses SMI features; Algo5 uses MACD-family features. All retain US-session and Schwab heritage.
- **Verified:** Sparkie is a research/selection workflow, not a distinct execution strategy. It sources US symbols, evaluates strategy/interval candidates, ranks results, gates a Replay verification, and can create a bot. Its inputs, dollar goals, data sources, and weekly ET schedule require redesign for PSX.
- **Verified:** C3’s read-only PSX SQLite provider and daily Replay path are directly reusable. Daily OHLCV is enough to research daily variants, but not enough to validate intraday, spread, order-book, fill, or latency assumptions.
- **Verified:** Existing order/trade tables are application and broker state, not L1/L2 exchange data. The repository has no proved normalized PSX tick/L0/L1/L2 store.
- **Verified:** The development runtime separates web, PostgreSQL, optional Redis/worker, volume-backed data/models/logs, and a read-only PSX SQLite mount. Replay uses Celery when available and a detached orchestrator subprocess for the bar loop.

## Recommendation

Retain authentication, audit patterns, read-only historical provider, Replay presentation, and the common probability/risk concepts. Extract strategies behind pure interfaces only after a PSX execution model and canonical market-data contract exist. Redesign Sparkie, live execution, calendars, risk, sizing, and symbol discovery for PSX. Reject obsolete copies and any implication that historical US backtests validate PSX suitability.

The smallest safe next contract is a StockIntel collector with raw immutable capture, reconnect/sequence handling, licensing confirmation, and a no-production-data test mode. Do not activate the 30-day trial before that contract is implemented and storage/coverage are ready.
