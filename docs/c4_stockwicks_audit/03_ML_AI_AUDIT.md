# ML and AI Audit

## Verified trained-model inference

Algo1–5 load/train scikit-learn `HistGradientBoostingClassifier` packages using joblib. Relevant paths:

- `app/scripts/ml/mm_live_helpers.py`: train, serialize, load, probability inference.
- `app/scripts/ml/model_refresh_policy.py`: artifact freshness and retraining.
- `app/scripts/research/Featureset_1.py` … `Featureset_5.py`: feature and label construction.
- `app/scripts/stock_algos/Algo1_MM.py` … `Algo5_MM.py`: runtime artifact naming and probability use.
- `app/scripts/stock_algos/algoMM_replay_runner.py`: Replay inference/cache path.

Artifacts are expected under configured `MODEL_DIR` and are named by algorithm/feature set/symbol/interval/forward horizon. No model artifact is tracked in the audited tree. Consequently, model provenance, exact training dates, metrics, and reproducibility for deployed artifacts are **unresolved**.

## Feature families

- Featureset 1: MA slope, momentum, RSI14, MACD histogram, volume expansion/ratio, support/resistance distance, regime, entropy/chop, price position, VWAP deviation.
- Featureset 2: Featureset 1 plus OBV and VWAP distance/direction/reclaim/reject behavior.
- Featureset 3: SMI K, D, histogram, slope.
- Featureset 4: same listed 12 variables as Featureset 1, through a divergent implementation.
- Featureset 5: ATR-normalized standard/fast/slow MACD lines, histograms, slopes, crosses, ranks, alignment, and EMA distance.

Inputs are OHLCV-derived. Several ratios/ATR-normalized values are scale-resistant; absolute P&L guardrails are currency-sensitive. VWAP on daily OHLCV is not equivalent to session VWAP from intraday trades.

## Other “AI” classifications

| Component | Classification | Evidence |
|---|---|---|
| `AI_algo2_MACD.py` | Deterministic indicator rules; misleading name | SMI crossing thresholds, no model load |
| `adaptive_predictor.py` | Deterministic adaptive weighted voting | OBV/VWAP/structure/volume/momentum reliability updates |
| `prediction_strategies.py` | Mixed trained-model experimental framework | joblib HGB models and multiple inference modes |
| `analysis_engine.py`, `chart_analysis_daytrade.py` | LLM-assisted/incomplete deployment path | hardcoded local Mistral GGUF path |
| Backtest evaluator files | Trained-model experiments | HGB/SGD/calibration training and joblib writes |
| Sparkie | Not itself ML | orchestrates candidate evaluation/ranking and Replay |

## Risks and PSX requirements

- Mandatory PSX retraining: symbol distributions, daily/intraday horizons, price/volume behavior, session structure, and corporate actions differ.
- Leakage risks to test: forward-label boundary, feature/training overlap with evaluated range, model auto-training during Replay, same-bar execution, adjusted-price consistency, and cross-validation chronology.
- Artifact contract required: dataset fingerprint, symbol universe, exchange calendar, interval, price-adjustment mode, feature schema/version, target horizon, train/validation dates, library versions, seed, calibration, and metrics.
- Do not load unknown joblib artifacts across trust boundaries; joblib/pickle deserialization can execute code.
- Existing results do not establish suitability or profitability for PSX.
