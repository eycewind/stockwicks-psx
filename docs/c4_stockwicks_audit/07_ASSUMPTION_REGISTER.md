# Assumption Register

| Evidence/value | Meaning | Sensitivity | PSX treatment |
|---|---|---|---|
| `US/Eastern`, 09:30–16:00/15:55; EOD 15:50/15:58 | US regular session and close | calendar/market structure | Replace with PSX calendar/session service |
| CME Sunday/Friday/17:00 rules | futures lifecycle | market structure | Remove from PSX equities |
| Celery timezone America/New_York | schedule basis | DST | Configure exchange timezone; store UTC |
| `$`, `stop_loss_usd=300`, trail `75`, P&L goals | USD risk | currency/account scale | Normalize to percent/ATR/bps and configurable PKR budgets |
| `k_forward=3`, builder 30 days | target horizon/warm-up | timeframe/sample size | Recalibrate per interval; never mechanically retain |
| long 0.60 / short 0.40, 3-bar smoothing/confirmation | classifier decision band | model calibration | Revalidate out of sample |
| short selling default/flags | borrow availability | regulation/account | Disable until PSX borrow/rules modeled |
| integer quantity | whole-share sizing | lot rules | Add PSX market-lot and tick-size rules |
| EOD forced close | intraday strategy policy | settlement/session | Strategy-specific configuration |
| Schwab intervals and 20-year daily request | provider contract | vendor | Remove from strategy modules |
| 1–30 minute beat ticks | expected bar availability | latency/timeframe | Derive from feed/session |
| prices multiplied by quantity | linear frictionless P&L | fees/slippage/taxes | PSX execution/fee model required |
| immediate/same-bar Replay fills | simplified execution | look-ahead/liquidity | next-event fill policy with bid/ask and volume constraints |
| adjusted daily prices | corporate action continuity | price mode | Persist adjustment metadata; forbid mixing modes |
| symbol `.upper()` and US lists/OCC syntax | identifier format | exchange/domain | Canonical `(exchange, instrument_id, symbol)` |
| VWAP from OHLCV/resampling | volume-weighted reference | data granularity | Label proxy; use trades for true session VWAP |
| volume/liquidity thresholds in rankers | US liquidity proxy | market scale | Normalize by PSX turnover/free float |
| no missing-session model | continuous expected samples | suspensions/holidays | Explicit no-trade/suspension states |
| option multiplier/0DTE expiry logic | US listed options | product structure | Reject for PSX equity MVP |

## Corporate actions, settlement, and fills

C3 supplies adjusted/raw price modes, but strategy artifacts do not encode that mode consistently. Settlement, price limits, circuit breakers, auction phases, taxes/fees, partial fills, queue priority, market lots, and broker buying power were not proved in the common engine. These are **Unresolved** inputs to the PSX execution contract, not values to guess.
