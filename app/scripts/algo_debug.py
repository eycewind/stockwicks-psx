# app/scripts/algo_debug.py
# app/scripts/algo_debug.py
import argparse
import pytz
from datetime import datetime
from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade
from app.utils.stock.market_time import max_bar_age_for_interval
from app.utils.stock import schwab_price_history as sph
from app.utils.stock.indicators import compute_smi

_ET = pytz.timezone("US/Eastern")

def fetch_df(symbol, interval):
    fn = {
        "1min": sph.get_schwab_1min, "5min": sph.get_schwab_5min,
        "15min": sph.get_schwab_15min, "30min": sph.get_schwab_30min,
        "1d": sph.get_schwab_daily,
    }.get(interval)
    if not fn:
        raise SystemExit(f"Unsupported interval {interval}")
    df = fn(symbol)
    if df is None or df.empty:
        print("[DEBUG] no data")
        return None
    return df

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-id", type=int)
    ap.add_argument("--symbol")
    ap.add_argument("--interval", default="1min")
    args = ap.parse_args()

    if args.bot_id:
        db = SessionLocal()
        bot = db.query(PaperStockTradeBot).filter_by(id=args.bot_id).first()
        if not bot:
            raise SystemExit("bot not found")
        symbol = (bot.symbol or "").upper()
        interval = (bot.interval or "1min").lower().strip()
        open_trade = db.query(PaperStockBotOpenTrade).filter_by(bot_id=bot.id).first()
        print(f"[BOT] {bot.id} {symbol}@{interval} algo={bot.algo_name} size={bot.trade_size} open={bool(open_trade)}")
        db.close()
    else:
        symbol = (args.symbol or "").upper()
        interval = (args.interval or "1min").lower().strip()

    df = fetch_df(symbol, interval)
    if df is None:
        return
    print(f"[DATA] {symbol}@{interval} bars={len(df)} latest={df.index[-1]} tz={df.index.tz}")

    now_et = datetime.now(_ET)
    latest = df.index[-1]
    if latest.tzinfo:
        age = now_et - latest.astimezone(_ET)
    else:
        age = now_et - _ET.localize(latest)
    max_age = max_bar_age_for_interval(interval)
    print(f"[DATA] age={age}, max_age={max_age}, stale={age>max_age}")

    smi = compute_smi(df)
    if smi is None or len(smi) < 5:
        print(f"[SMI] not enough points (len={len(smi) if smi is not None else 0})")
    else:
        print("[SMI] last 5:", [round(float(x), 2) for x in smi.tail(5).tolist()])

    def cross_up(series, level=-60.0, lookback=3):
        for k in range(1, min(lookback, len(series)-1)+1):
            if float(series.iloc[-(k+1)]) < level <= float(series.iloc[-k]):
                return True, k
        return False, None

    def cross_dn(series, level=+60.0, lookback=3):
        for k in range(1, min(lookback, len(series)-1)+1):
            if float(series.iloc[-(k+1)]) > level >= float(series.iloc[-k]):
                return True, k
        return False, None

    if smi is not None and len(smi) >= 5:
        le, lek = cross_up(smi, -60, 3)
        se, sek = cross_dn(smi, +60, 3)
        lx, lxk = cross_dn(smi, +60, 3)
        sx, sxk = cross_up(smi, -60, 3)
        print(f"[SIG] long_entry={le} ({lek}), short_entry={se} ({sek}), long_exit={lx} ({lxk}), short_exit={sx} ({sxk})")
        print(f"[PX ] last close={float(df['close'].iloc[-1]):.4f}")

if __name__ == "__main__":
    main()
