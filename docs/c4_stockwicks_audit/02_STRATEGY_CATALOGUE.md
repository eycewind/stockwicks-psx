# Strategy and Bot Catalogue

## Canonical active stock families

| Family | Canonical implementation | Decision logic / inputs | Runtime | PSX finding |
|---|---|---|---|---|
| Algo1_MM | `app/scripts/stock_algos/Algo1_MM.py` + `Featureset_1.py` | HGB upward probability; 12 normalized/technical features; smoothed threshold entry; common exits | explicit dispatch in `stock_bot_runner.py` | Extract/adapt; daily OHLCV sufficient for daily research |
| Algo2_MM | `Algo2_MM.py` + `Featureset_2.py` | Algo1 plus OBV and VWAP behavior features | explicit dispatch | Extract/adapt; VWAP meaning changes materially on daily bars |
| Algo3_MM | `Algo3_MM.py` + `Featureset_3.py` | HGB on SMI K/D/histogram/slope | explicit dispatch | Extract/adapt and retrain |
| Algo4_MM | `Algo4_MM.py` + `Featureset_4.py` | older/independent implementation with the same 12-feature family and common core concepts | explicit dispatch | Prefer rejection or consolidation after parity tests |
| Algo5_MM | `Algo5_MM.py` + `Featureset_5.py` | HGB on ATR-normalized multi-speed MACD state/cross/slope features | explicit dispatch | Best technical portability, still mandatory retraining |
| Algo_SMI | `algo3_runner.py` | deterministic SMI threshold/cross logic | lazy dispatch | Extract/adapt; validate daily semantics |
| Algo_MACD | `algo2_runner.py` | deterministic MACD runner | lazy dispatch | Extract/adapt; validate daily semantics |
| fallback/legacy Algo1–3 | `algo_runner.py`, runner wrappers | legacy deterministic route selected for unknown names | fallback dispatch | Unresolved/retire from canonical UI |

Common Algo1–5 defaults include 30 builder days, `k_forward=3`, long probability 0.60, short 0.40, probability smoothing and confirmation, absolute USD stops/trails, optional shorting, and EOD close. `mm_core_engine.py` implements directional LONG/SHORT entry and position exits. There are no bid/ask quote pairs, inventory targets, spread models, or passive maker orders. **Verified conclusion: MM here is a project label, not market making.**

## Entry, exit, state, and execution

- Training label: forward close direction over `k_forward`; sample weighting is used.
- Entry: smoothed `prob_up`; long above threshold and short below threshold after confirmation and minimum edge.
- Exit: absolute or percentage stop, trailing profit, probability fixed/trailing exit, and EOD close.
- State: bot configuration in PostgreSQL/JSON; open and historical positions in dedicated tables; model and logs on disk.
- Sizing: integer quantity/trade-size fields, often dollar-goal assumptions in Sparkie.
- Output: `place_paper_and_maybe_live_order`/paper service; optional notification and Schwab live mirror.

## Replay and backtests

`algoMM_replay_runner.py` maps Algo1–5 to the same feature families and common decision core. Replay consumes cached CSV through `ReplayDataProvider`; C3 enables canonical `1d` PSX data. Backtest/evaluation variants under `app/scripts/stocks/backtest_algos` are experiments, not canonical runtime merely because names resemble live files. Same-bar decisions/fills and retraining boundaries need formal leakage tests before product claims.

## Rankers and selectors

- `symbol_screener.py` and `algoMM_blind_symbol_ranker.py` train/evaluate classifiers and rank US-symbol candidates.
- Barchart and Stocktwits services supply US-oriented lists.
- Sparkie selects symbols, intervals, Algo1–5 candidates, ranks backtest evidence, then opens exact Replay verification.
- These are research orchestration components; none is an exchange strategy.

## RSI, SMI, MACD, options

RSI is a feature within Feature sets 1/2/4 and appears in experimental scripts; no separately proved active `Algo_RSI` dispatch exists. `AI_algo2_MACD.py` is misleadingly named: its documented/implemented path is deterministic SMI crossing, not model inference. Numerous options/SPX scripts use option chains, Greeks/OI, OCC symbols, 100-share multipliers, and US expiry rules; the mounted SPX 0DTE product is not reusable for PSX equities.

## Duplicates and coverage

Copies, `_old`, `_updated`, evaluator, optimizer, and standalone runner files are evidence of experimentation, not distinct products. Unit tests cover common decision logic, runners, Replay, C2/C3 contracts, and Sparkie services, but the audit found no evidence establishing out-of-sample PSX profitability or realistic PSX fills.
