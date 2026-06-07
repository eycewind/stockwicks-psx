#!/usr/bin/env python3
"""
backtest_adaptive.py — Backtest the Adaptive Predictor on real data
====================================================================

Fetches real 1-min data from Schwab and replays it bar-by-bar through
the AdaptivePredictor. Simulates trades with the same entry/exit logic
as the live runner.

Usage:
  python -m app.scripts.stock_algos.backtest_adaptive --symbols MU,TSLA,AMD --days 3
  python -m app.scripts.stock_algos.backtest_adaptive --symbols TSLA --days 2 --verbose
"""

import os, sys, argparse, json, logging
from datetime import datetime, time as dtime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from collections import defaultdict

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Import the adaptive predictor
from app.scripts.stock_algos.adaptive_predictor import AdaptivePredictor, PredictionResult

logger = logging.getLogger("Backtest")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [Backtest] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ============================================================================
# TRADE TRACKING
# ============================================================================

@dataclass
class BacktestTrade:
    symbol: str
    side: str                # "LONG" or "SHORT"
    entry_price: float
    entry_bar: int
    entry_time: str
    entry_confidence: float
    exit_price: float = 0.0
    exit_bar: int = 0
    exit_time: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    bars_held: int = 0
    max_favorable: float = 0.0    # best P&L during trade
    max_adverse: float = 0.0      # worst P&L during trade


# ============================================================================
# BACKTEST ENGINE
# ============================================================================

class BacktestEngine:
    """
    Simulates trading with the AdaptivePredictor on historical data.
    
    Entry: direction != 0 AND confidence >= min_confidence AND min_predictions met
    Exit:  confidence drops below stop_confidence
           OR OBV flips against position
           OR 1% per-share stop loss
           OR EOD close at 15:55
    """
    
    def __init__(
        self,
        min_confidence: float = 70.0,
        stop_confidence: float = 50.0,
        per_share_stop_pct: float = 0.01,
        min_predictions: int = 5,
        cooldown_bars: int = 2,
        max_same_dir_losses: int = 3,
        skip_first_n_bars: int = 5,    # skip first 5 min for warmup
    ):
        self.min_confidence = min_confidence
        self.stop_confidence = stop_confidence
        self.per_share_stop_pct = per_share_stop_pct
        self.min_predictions = min_predictions
        self.cooldown_bars = cooldown_bars
        self.max_same_dir_losses = max_same_dir_losses
        self.skip_first_n_bars = skip_first_n_bars
    
    def run(
        self,
        df: pd.DataFrame,
        symbol: str,
        verbose: bool = False,
    ) -> Dict:
        """
        Run backtest on a DataFrame of 1-min OHLCV bars.
        Returns detailed results dict.
        """
        predictor = AdaptivePredictor(
            min_confidence=self.min_confidence,
            stop_confidence=self.stop_confidence,
            min_predictions=self.min_predictions,
        )
        
        trades: List[BacktestTrade] = []
        position: Optional[BacktestTrade] = None
        bars_since_exit = 999
        prediction_log = []
        
        # Track direction losses for limit
        dir_losses = {"LONG": 0, "SHORT": 0}
        last_day = None
        
        n = len(df)
        
        for i in range(n):
            price = float(df["close"].iloc[i])
            ts = str(df.index[i])
            
            # Get time for session checks
            bar_time = None
            bar_date = None
            try:
                bar_time = df.index[i].time()
                bar_date = df.index[i].date()
            except Exception:
                pass
            
            # Reset predictor and direction losses each new day
            if bar_date and bar_date != last_day:
                predictor.reset()
                dir_losses = {"LONG": 0, "SHORT": 0}
                last_day = bar_date
                bars_since_exit = 999
            
            # Skip pre-market and post-market
            if bar_time and (bar_time < dtime(9, 30) or bar_time > dtime(16, 0)):
                continue
            
            # Get prediction
            result = predictor.tick(df, i)
            
            # Log prediction
            prediction_log.append({
                "bar": i, "time": ts, "price": price,
                "direction": result.direction,
                "confidence": result.confidence,
                "accuracy": result.prediction_accuracy,
                "regime": result.regime,
                "votes": result.vote_sum,
                "obv": result.obv_vote,
                "vwap": result.vwap_vote,
                "struct": result.structure_vote,
                "vol": result.volume_vote,
                "mom": result.momentum_vote,
                "should_trade": result.should_trade,
                "in_position": position is not None,
            })
            
            # ── EXIT LOGIC ──
            if position is not None:
                position.bars_held = i - position.entry_bar
                
                # Track max favorable/adverse excursion
                if position.side == "LONG":
                    running_pnl = price - position.entry_price
                else:
                    running_pnl = position.entry_price - price
                position.max_favorable = max(position.max_favorable, running_pnl)
                position.max_adverse = min(position.max_adverse, running_pnl)
                
                should_exit = False
                exit_reason = ""
                
                # 1. Per-share stop (1% of entry)
                stop_price = position.entry_price * self.per_share_stop_pct
                per_share_loss = -running_pnl if running_pnl < 0 else 0
                if per_share_loss >= stop_price:
                    should_exit = True
                    exit_reason = "STOP_LOSS"
                
                # 2. OBV flipped against position
                if not should_exit:
                    if position.side == "LONG" and result.obv_vote == -1:
                        should_exit = True
                        exit_reason = "OBV_FLIP"
                    elif position.side == "SHORT" and result.obv_vote == 1:
                        should_exit = True
                        exit_reason = "OBV_FLIP"
                
                # 3. Confidence dropped below stop level
                if not should_exit and result.confidence < self.stop_confidence:
                    if position.bars_held >= 2:  # give it 2 bars minimum
                        should_exit = True
                        exit_reason = "LOW_CONFIDENCE"
                
                # 4. Direction flipped with confidence
                if not should_exit:
                    if position.side == "LONG" and result.direction == -1 and result.confidence > 60:
                        should_exit = True
                        exit_reason = "DIRECTION_FLIP"
                    elif position.side == "SHORT" and result.direction == 1 and result.confidence > 60:
                        should_exit = True
                        exit_reason = "DIRECTION_FLIP"
                
                # 5. EOD close
                if not should_exit and bar_time and bar_time >= dtime(15, 55):
                    should_exit = True
                    exit_reason = "EOD"
                
                if should_exit:
                    if position.side == "LONG":
                        position.pnl = price - position.entry_price
                    else:
                        position.pnl = position.entry_price - price
                    position.exit_price = price
                    position.exit_bar = i
                    position.exit_time = ts
                    position.exit_reason = exit_reason
                    trades.append(position)
                    
                    # Track direction losses
                    if position.pnl < 0:
                        dir_losses[position.side] += 1
                    else:
                        dir_losses[position.side] = 0  # reset on win
                    
                    if verbose:
                        icon = "✅" if position.pnl > 0 else "❌"
                        print(
                            f"  {icon} EXIT {position.side} ${position.entry_price:.2f}→"
                            f"${price:.2f} P&L=${position.pnl:+.2f} "
                            f"held={position.bars_held}bars reason={exit_reason} "
                            f"mfe=${position.max_favorable:+.2f} mae=${position.max_adverse:+.2f}"
                        )
                    
                    position = None
                    bars_since_exit = 0
                    continue
            
            # ── ENTRY LOGIC ──
            else:
                bars_since_exit += 1
                
                # Cooldown
                if bars_since_exit < self.cooldown_bars:
                    continue
                
                # Don't enter near close
                if bar_time and bar_time >= dtime(15, 50):
                    continue
                
                # Skip warmup bars at start of day
                if i < self.skip_first_n_bars:
                    continue
                
                # Direction loss limit
                direction = "LONG" if result.direction == 1 else "SHORT" if result.direction == -1 else None
                if direction and dir_losses.get(direction, 0) >= self.max_same_dir_losses:
                    continue
                
                # Enter if predictor says to trade
                if result.should_trade and result.direction != 0:
                    side = "LONG" if result.direction == 1 else "SHORT"
                    position = BacktestTrade(
                        symbol=symbol,
                        side=side,
                        entry_price=price,
                        entry_bar=i,
                        entry_time=ts,
                        entry_confidence=result.confidence,
                    )
                    
                    if verbose:
                        print(
                            f"  → ENTRY {side} ${price:.2f} "
                            f"conf={result.confidence:.0f}% "
                            f"votes={result.vote_sum} "
                            f"acc={result.prediction_accuracy:.0f}% "
                            f"regime={result.regime}"
                        )
        
        # Close any open position at end
        if position is not None and n > 0:
            price = float(df["close"].iloc[-1])
            if position.side == "LONG":
                position.pnl = price - position.entry_price
            else:
                position.pnl = position.entry_price - price
            position.exit_price = price
            position.exit_bar = n - 1
            position.exit_time = str(df.index[-1])
            position.exit_reason = "SESSION_END"
            trades.append(position)
        
        # ── Compute stats ──
        return self._compute_stats(symbol, trades, prediction_log, df)
    
    def _compute_stats(self, symbol, trades, prediction_log, df):
        tt = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        
        total_pnl = sum(t.pnl for t in trades)
        win_rate = len(wins) / tt * 100 if tt > 0 else 0
        
        gp = sum(t.pnl for t in wins)
        gl = abs(sum(t.pnl for t in losses))
        pf = gp / gl if gl > 0 else (99.0 if gp > 0 else 0)
        
        avg_bars = sum(t.bars_held for t in trades) / tt if tt > 0 else 0
        avg_mfe = sum(t.max_favorable for t in trades) / tt if tt > 0 else 0
        avg_mae = sum(t.max_adverse for t in trades) / tt if tt > 0 else 0
        
        # Exit reason breakdown
        exit_reasons = defaultdict(int)
        for t in trades:
            exit_reasons[t.exit_reason] += 1
        
        # Prediction accuracy
        total_preds = sum(1 for p in prediction_log if p["direction"] != 0)
        
        # By-day breakdown
        by_day = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
        for t in trades:
            day = t.entry_time[:10] if len(t.entry_time) >= 10 else "unknown"
            by_day[day]["trades"] += 1
            by_day[day]["pnl"] += t.pnl
            if t.pnl > 0:
                by_day[day]["wins"] += 1
        
        return {
            "symbol": symbol,
            "total_trades": tt,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(pf, 2),
            "avg_win": round(gp / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0,
            "biggest_win": round(max((t.pnl for t in trades), default=0), 2),
            "biggest_loss": round(min((t.pnl for t in trades), default=0), 2),
            "avg_bars_held": round(avg_bars, 1),
            "avg_mfe": round(avg_mfe, 2),
            "avg_mae": round(avg_mae, 2),
            "exit_reasons": dict(exit_reasons),
            "by_day": dict(by_day),
            "total_predictions": total_preds,
            "trades": [
                {
                    "side": t.side,
                    "entry": round(t.entry_price, 2),
                    "exit": round(t.exit_price, 2),
                    "pnl": round(t.pnl, 2),
                    "bars": t.bars_held,
                    "reason": t.exit_reason,
                    "confidence": round(t.entry_confidence, 1),
                    "entry_time": t.entry_time,
                    "mfe": round(t.max_favorable, 2),
                    "mae": round(t.max_adverse, 2),
                }
                for t in trades
            ],
        }


# ============================================================================
# DATA FETCHING
# ============================================================================

def fetch_data(symbol: str, days: int = 3) -> Optional[pd.DataFrame]:
    """Fetch 1-min data from Schwab using existing infrastructure."""
    try:
        from app.scripts.stock_algos.base_wiring import StockBaseRunner
        runner = StockBaseRunner()
        df = runner.fetch_source_bars(symbol, interval="5min", lookback_days=days)
        if df is None or df.empty:
            logger.error(f"No data for {symbol}")
            return None
        
        # Ensure ET timezone
        import pytz
        ET = pytz.timezone("US/Eastern")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(ET)
        else:
            df.index = df.index.tz_convert(ET)
        
        # Filter to market hours
        df = df.between_time("09:30", "16:00")
        
        logger.info(f"{symbol}: {len(df)} bars from {df.index[0]} to {df.index[-1]}")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch {symbol}: {e}")
        return None


# ============================================================================
# MAIN
# ============================================================================

def run_backtest(
    symbols: List[str],
    days: int = 3,
    verbose: bool = False,
    min_confidence: float = 70.0,
):
    engine = BacktestEngine(
        min_confidence=min_confidence,
        stop_confidence=50.0,
        per_share_stop_pct=0.01,
    )
    
    all_results = []
    
    for symbol in symbols:
        print(f"\n{'='*80}")
        print(f"  BACKTEST: {symbol} ({days} days, 1-min bars)")
        print(f"{'='*80}")
        
        df = fetch_data(symbol, days)
        if df is None:
            continue
        
        result = engine.run(df, symbol, verbose=verbose)
        all_results.append(result)
        
        # Print results
        r = result
        print(f"\n{'─'*60}")
        print(f"  {symbol} RESULTS")
        print(f"{'─'*60}")
        print(f"  Trades: {r['total_trades']} ({r['wins']}W / {r['losses']}L)")
        print(f"  Win Rate: {r['win_rate']}%")
        print(f"  P&L: ${r['total_pnl']:+.2f}")
        print(f"  Profit Factor: {r['profit_factor']}")
        print(f"  Avg Win: ${r['avg_win']:+.2f} | Avg Loss: ${r['avg_loss']:+.2f}")
        print(f"  Biggest Win: ${r['biggest_win']:+.2f} | Biggest Loss: ${r['biggest_loss']:+.2f}")
        print(f"  Avg Bars Held: {r['avg_bars_held']}")
        print(f"  Avg MFE: ${r['avg_mfe']:+.2f} | Avg MAE: ${r['avg_mae']:+.2f}")
        print(f"  Exit Reasons: {r['exit_reasons']}")
        
        # By day
        print(f"\n  By Day:")
        for day in sorted(r['by_day']):
            d = r['by_day'][day]
            wr = d['wins'] / d['trades'] * 100 if d['trades'] > 0 else 0
            print(f"    {day}: {d['trades']}T {d['wins']}W {wr:.0f}%WR ${d['pnl']:+.2f}")
        
        # Trade list
        if r['trades']:
            print(f"\n  Trade Log:")
            for i, t in enumerate(r['trades']):
                icon = "✅" if t['pnl'] > 0 else "❌"
                time_short = t['entry_time'].split(" ")[-1][:8] if " " in t['entry_time'] else t['entry_time']
                print(
                    f"    {icon} {t['side']:5s} ${t['entry']:.2f}→${t['exit']:.2f} "
                    f"P&L=${t['pnl']:+.2f} {t['bars']}bars "
                    f"conf={t['confidence']:.0f}% {t['reason']}"
                )
    
    # Summary across all symbols
    if len(all_results) > 1:
        print(f"\n{'='*80}")
        print(f"  OVERALL SUMMARY")
        print(f"{'='*80}")
        
        total_trades = sum(r['total_trades'] for r in all_results)
        total_wins = sum(r['wins'] for r in all_results)
        total_pnl = sum(r['total_pnl'] for r in all_results)
        
        print(f"  Symbols: {len(all_results)}")
        print(f"  Total Trades: {total_trades}")
        print(f"  Total Wins: {total_wins} ({total_wins/total_trades*100:.0f}% WR)" if total_trades > 0 else "")
        print(f"  Total P&L: ${total_pnl:+.2f}")
        
        print(f"\n  Per Symbol:")
        for r in sorted(all_results, key=lambda x: x['total_pnl'], reverse=True):
            icon = "✅" if r['total_pnl'] > 0 else "❌"
            print(
                f"    {icon} {r['symbol']:<6} {r['total_trades']}T "
                f"{r['win_rate']}%WR ${r['total_pnl']:+.2f} PF={r['profit_factor']}"
            )
    
    # Save results
    output_path = "/var/www/stockwicks/data/backtest_adaptive_results.json"
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump({
                "run_at": datetime.now().isoformat(),
                "days": days,
                "min_confidence": min_confidence,
                "results": all_results,
            }, f, indent=2, default=str)
        print(f"\nResults saved to {output_path}")
    except Exception as e:
        logger.warning(f"Failed to save: {e}")
    
    return all_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Adaptive Predictor")
    parser.add_argument("--symbols", type=str, default="MU,TSLA,AMD",
                        help="Comma-separated symbols")
    parser.add_argument("--days", type=int, default=3,
                        help="Number of days to backtest")
    parser.add_argument("--verbose", action="store_true",
                        help="Print every trade entry/exit")
    parser.add_argument("--confidence", type=float, default=70.0,
                        help="Minimum confidence to trade")
    args = parser.parse_args()
    
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    run_backtest(symbols, args.days, args.verbose, args.confidence)
