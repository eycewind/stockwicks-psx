import os
import csv
import argparse
from decimal import Decimal
from datetime import datetime
from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockBotTradeHistory  # has exit_time, profit_loss, etc.

EXPORT_DIR = os.path.join("app", "data", "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

def dec(x):
    if x is None:
        return Decimal("0")
    try:
        d = Decimal(str(x))
        return d if d.is_finite() else Decimal("0")
    except Exception:
        return Decimal("0")

def export_user_trades(user_id: int, start: datetime | None = None, end: datetime | None = None):
    db = SessionLocal()
    try:
        q = db.query(PaperStockBotTradeHistory).filter(
            PaperStockBotTradeHistory.user_id == user_id
        )
        # The history table uses exit_time for the close timestamp
        if start:
            q = q.filter(PaperStockBotTradeHistory.exit_time >= start)
        if end:
            q = q.filter(PaperStockBotTradeHistory.exit_time <= end)

        q = q.order_by(PaperStockBotTradeHistory.exit_time.asc())
        rows = q.all()

        trades = []
        wins = losses = breakeven = 0
        net = Decimal("0")
        biggest_win = Decimal("-1e18")
        biggest_loss = Decimal("1e18")

        for r in rows:
            pl = dec(r.profit_loss)
            net += pl
            if pl > 0: wins += 1
            elif pl < 0: losses += 1
            else: breakeven += 1
            if pl > biggest_win: biggest_win = pl
            if pl < biggest_loss: biggest_loss = pl

            trades.append({
                "bot_id": r.bot_id,
                "symbol": r.symbol,
                "side": r.position_side,
                "qty": r.quantity,
                "entry_price": r.entry_price,
                "exit_price": r.exit_price,
                "pl": float(pl),
                "exit_time": r.exit_time.isoformat() if r.exit_time else None,
                "algo": getattr(r, "algo_name", None),
                "trade_type": r.trade_type,
            })

        total = len(trades)
        win_rate = (wins / total * 100) if total else 0.0

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"user_{user_id}_trades_{ts}.csv"
        filepath = os.path.join(EXPORT_DIR, filename)

        fieldnames = list(trades[0].keys()) if trades else [
            "bot_id","symbol","side","qty","entry_price","exit_price","pl","exit_time","algo","trade_type"
        ]
        with open(filepath, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(trades)

        print("✅ Export complete!")
        print(f"Saved to: {filepath}")
        print(f"\nSummary for user {user_id}:")
        print(f"  Trades: {total}")
        print(f"  Wins: {wins}  Losses: {losses}  Breakeven: {breakeven}")
        print(f"  Win rate: {win_rate:.1f}%")
        print(f"  Net P/L: ${net:.2f}")
        print(f"  Biggest Win: ${biggest_win:.2f}")
        print(f"  Biggest Loss: ${biggest_loss:.2f}")
        return filepath
    finally:
        db.close()

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Export user trading history to CSV")
    p.add_argument("--user", type=int, required=True, help="User ID")
    p.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)", default=None)
    p.add_argument("--end", type=str, help="End date (YYYY-MM-DD)", default=None)
    a = p.parse_args()

    start_dt = datetime.strptime(a.start, "%Y-%m-%d") if a.start else None
    end_dt = datetime.strptime(a.end, "%Y-%m-%d") if a.end else None

    export_user_trades(user_id=a.user, start=start_dt, end=end_dt)
