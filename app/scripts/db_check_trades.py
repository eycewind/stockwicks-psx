import argparse
from datetime import datetime, timedelta

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockBotTradeHistory


def main():
    ap = argparse.ArgumentParser(description="Check paper bot trades from DB")
    ap.add_argument("--user-id", type=int, required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--date", required=True, help="Date in YYYY-MM-DD")
    args = ap.parse_args()

    session = SessionLocal()

    start_date = datetime.strptime(args.date, "%Y-%m-%d")
    end_date = start_date + timedelta(days=1)

    print(f"🔍 Checking PaperStockBotTradeHistory for user={args.user_id}, symbol={args.symbol}, date={args.date}")

    trades = (
        session.query(PaperStockBotTradeHistory)
        .filter(PaperStockBotTradeHistory.user_id == args.user_id)
        .filter(PaperStockBotTradeHistory.symbol == args.symbol.upper())
        .filter(PaperStockBotTradeHistory.entry_time >= start_date)
        .filter(PaperStockBotTradeHistory.entry_time < end_date)
        .all()
    )

    if trades:
        print(f"\n📌 Found {len(trades)} trades:")
        for t in trades:
            print(
                f"ID={t.id} | Bot={t.bot_id} ({t.algo_name}) | {t.trade_type} {t.quantity} "
                f"{t.symbol} | entry={t.entry_price} at {t.entry_time} "
                f"| exit={t.exit_price} at {t.exit_time} | P/L={t.profit_loss}"
            )
    else:
        print("\n❌ No trades found in PaperStockBotTradeHistory for this date.")


if __name__ == "__main__":
    main()
