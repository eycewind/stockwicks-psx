# app/scripts/stocks/AI_algo1_SMI.py
import os, sys, time, glob, logging
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.utils.stock.data_fetch import get_schwab_1min_history, process_interval
from app.utils.stock.indicators import compute_smi
from app.utils.stock.bot_trading import place_order
from app.services.email_service import send_email

log = logging.getLogger("AI_algo1_SMI")

intervals_to_test = ['1min','5min','15min','1d']
_INTERVAL_SEC = {"1min":60,"5min":300,"15min":900,"1d":86400}

# ========== Helpers ==========
def _last_closed_bar_time(now_utc, interval):
    sec = _INTERVAL_SEC[interval]; t = now_utc - timedelta(seconds=30)
    return pd.Timestamp(int(t.timestamp()) - int(t.timestamp()) % sec, unit="s", tz="UTC")

def _cross_up(series, level, lookback=3):
    for k in range(1, lookback+1):
        if series.iloc[-(k+1)] < level and series.iloc[-k] > level: return True
    return False

def _cross_down(series, level, lookback=3):
    for k in range(1, lookback+1):
        if series.iloc[-(k+1)] > level and series.iloc[-k] < level: return True
    return False

# ========== Backtest ==========
def backtest_smi(resampled, trade_size, pt=None, sl=None):
    smi = compute_smi(resampled); closes = resampled["close"].astype(float)
    trades=wins=0; pnl_sum=0.0; pos=None; entry=None

    for i in range(1,len(resampled)):
        smi_win = smi.iloc[:i+1]; px=float(closes.iloc[i])
        if pos is None:
            if _cross_up(smi_win,-60): pos,entry="long",px
            elif _cross_down(smi_win,60): pos,entry="short",px
            continue

        # exit logic
        if pos=="long":
            if _cross_down(smi_win,60) or (pt and (px-entry)>=pt) or (sl and (px-entry)<=-sl):
                pnl=(px-entry)*trade_size; pnl_sum+=pnl; trades+=1; wins+=(pnl>0); pos=None
        elif pos=="short":
            if _cross_up(smi_win,-60) or (pt and (entry-px)>=pt) or (sl and (entry-px)<=-sl):
                pnl=(entry-px)*trade_size; pnl_sum+=pnl; trades+=1; wins+=(pnl>0); pos=None

    sr=(wins/trades*100) if trades>0 else 0
    return {"trades":trades,"wins":wins,"total_return":pnl_sum,"success_rate":sr}

# ========== Main Evaluation ==========
def evaluate(symbol, trade_size, user_id, pt=None, sl=None, live=False, email=None):
    df = get_schwab_1min_history(symbol)
    if df is None or df.empty: return None
    cutoff=min(_last_closed_bar_time(datetime.now(timezone.utc),iv) for iv in intervals_to_test)

    results=[]
    for iv in intervals_to_test:
        _,_,resampled=process_interval(df,iv,symbol)
        resampled=resampled[resampled.index<=cutoff]
        if resampled is None or len(resampled)<5: continue
        stats=backtest_smi(resampled,trade_size,pt,sl)
        results.append({
            "algo":"SMI","interval":iv,"total_return":round(stats["total_return"],2),
            "success_rate":round(stats["success_rate"],2),
            "trades":stats["trades"],
            "trade_span":"Day Trade" if iv in {"1min","5min"} else "Swing Trade"
        })

    if not results: return None
    dfres=pd.DataFrame(results).sort_values(by=['success_rate','total_return'],ascending=[False,False])
    best=dfres.iloc[0]

    # Save output
    outpath=f"/var/www/stockwicks/data/{user_id}/{user_id}_{symbol}_AI_algo1_SMI.csv"
    pd.DataFrame([best]).to_csv(outpath,index=False)
    log.info(f"Saved recommendation: {outpath}")

    # Trigger live order?
    if live:
        side="BUY" if "long" else "SELL"  # you'd derive from signal
        place_order(user_id, symbol, side, trade_size)
        log.info(f"Placed live order {side} {symbol}")

    # Send email?
    if email:
        send_email(email, f"Algo1 SMI Signal for {symbol}", f"Best interval: {best['interval']}, SR={best['success_rate']}%")

    return best
