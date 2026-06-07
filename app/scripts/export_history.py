# app/scripts/export_history.py
# app/scripts/export_history.py
import csv
from pathlib import Path
from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql://stockwicks_user:Stockwick2024@localhost:5432/stockwicks"

def export_history(user_id: int = 116):
    engine = create_engine(DATABASE_URL)
    cache_file = Path("app/cache/history.csv")

    # ✅ Ensure parent folder exists
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    with engine.connect() as conn:
        # Option bot trades
        option_query = text("""
            SELECT option_symbol, trade_type, position_side, quantity, entry_price, exit_price, pnl, entry_time, exit_time
            FROM paper_option_bot_trade_history
            WHERE user_id = :uid
            ORDER BY exit_time DESC
            LIMIT 50
        """)
        option_rows = conn.execute(option_query, {"uid": user_id}).fetchall()

        # Stock bot trades
        stock_query = text("""
            SELECT symbol, trade_type, position_side, quantity, entry_price, exit_price, profit_loss, entry_time, exit_time
            FROM paper_stock_bot_trade_history
            WHERE user_id = :uid
            ORDER BY exit_time DESC
            LIMIT 50
        """)
        stock_rows = conn.execute(stock_query, {"uid": user_id}).fetchall()

    # Write CSV
    with open(cache_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "symbol", "side", "qty", "entry", "exit", "pnl", "entry_time", "exit_time"])

        # Option rows
        for row in option_rows:
            writer.writerow([
                "OPTION",
                row.option_symbol, row.position_side, row.quantity,
                row.entry_price, row.exit_price, row.pnl,
                row.entry_time, row.exit_time
            ])

        # Stock rows
        for row in stock_rows:
            writer.writerow([
                "STOCK",
                row.symbol, row.position_side, row.quantity,
                row.entry_price, row.exit_price, row.profit_loss,
                row.entry_time, row.exit_time
            ])

    print(f"✅ History exported to {cache_file}")

if __name__ == "__main__":
    export_history()
