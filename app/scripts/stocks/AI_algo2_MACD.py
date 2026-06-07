# app/scripts/stocks/AI_algo2_MACD.py
import os, sys, logging
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.utils.stock.data_fetch import get_schwab_1min_history, process_interval
from app.utils.stock.bot_trading import place_order
from app.services.email_service import send_email

log = logging.getLogger("AI_algo2_MACD")
intervals_to_test = ['1min','5min','15min','1d']

def compute_macd(df, fast=12, slow=26, signal=9):
    c = df["close"].astype(float)
    ema_fast = c.ewm(span=fast, adjust=False).mean()
    ema_slow = c.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    return macd, macd_signal

def backtest_macd(resampled, trade_size, pt=None, sl=None):
    macd, signal = compute_macd(resampled)
    closes = resampled["close"].astype(float)
    pos=None; entry=None; trades=wins=0; pnl_sum=0.0

    for i in range(1,len(resampled)):
        px=float(closes.iloc[i])
        if pos is None:
            if macd.iloc[i-1]<signal.iloc[i-1] and macd.iloc[i]>signal.iloc[i]:
                pos,entry="long",px
            elif macd.iloc[i-1]>signal.iloc[i-1] and macd.iloc[i]<signal.iloc[i]:
                pos,entry="short",px
            continue

        if pos=="long":
            if (macd.iloc[i]<signal.iloc[i]) or (pt and (px-entry)>=pt) or (sl and (px-entry)<=-sl):
                pnl=(px-entry)*trade_size; pnl_sum+=pnl; trades+=1; wins+=(pnl>0); pos=None
        elif pos=="short":
            if (macd.iloc[i]>signal.iloc[i]) or (pt and (entry-px)>=pt) or (sl and (entry-px)<=-sl):
                pnl=(entry-px)*trade_size; pnl_sum+=pnl; trades+=1; wins+=(pnl>0); pos=None

    sr=(wins/trades*100) if trades>0 else 0
    return {"trades":trades,"wins":wins,"total_return":pnl_sum,"success_rate":sr}

def evaluate(symbol, trade_size, user_id, pt=None, sl=None, live=False, email=None):
    df = get_schwab_1min_history(symbol)
    if df is None or df.empty: return None
    results=[]
    for iv in intervals_to_test:
        _,_,resampled=process_interval(df,iv,symbol)
        if resampled is None or len(resampled)<35: continue
        stats=backtest_macd(resampled,trade_size,pt,sl)
        results.append({"algo":"MACD","interval":iv,"total_return":round(stats["total_return"],2),
                        "success_rate":round(stats["success_rate"],2),"trades":stats["trades"],
                        "trade_span":"Day Trade" if iv in {"1min","5min"} else "Swing Trade"})
    if not results: return None
    dfres=pd.DataFrame(results).sort_values(by=['success_rate','total_return'],ascending=[False,False])
    best=dfres.iloc[0]
    outpath=f"/var/www/stockwicks/data/{user_id}/{user_id}_{symbol}_AI_algo2_MACD.csv"
    pd.DataFrame([best]).to_csv(outpath,index=False)
    if live: place_order(user_id,symbol,"BUY" if best["success_rate"]>50 else "SELL",trade_size)
    if email: send_email(email,f"MACD Algo Signal {symbol}",str(best))
    return best
