
#/var/www/stockwicks/app/routes/plot_routes.py
# /var/www/stockwicks/app/routes/plot_routes.py
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
import os, time

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockBotTradeHistory
from app.models.trade_position import TradePosition
from app.utils.stock.indicators import compute_smi_blau, compute_smi_blau_smooth
from app.scripts.plot_bot_day import get_candles, plot, ET

router = APIRouter()

@router.get("/plot/{user_id}/{symbol}/{date}", response_class=HTMLResponse)
async def plot_user_trades(user_id: int, symbol: str, date: str, interval: str = "1min", days: int = 10):
    start = ET.localize(datetime.fromisoformat(date))
    end = start + timedelta(days=1)

    df = get_candles(symbol, interval, date, days)
    if df is None or df.empty:
        return HTMLResponse("<h3>No data found</h3>")

    k_series, d_series = compute_smi_blau(df)
    k_smooth, d_smooth = compute_smi_blau_smooth(df)

    session = SessionLocal()
    try:
        trades = (
            session.query(PaperStockBotTradeHistory)
            .filter(PaperStockBotTradeHistory.user_id == user_id)
            .filter(PaperStockBotTradeHistory.symbol == symbol.upper())
            .filter(PaperStockBotTradeHistory.entry_time >= start)
            .filter(PaperStockBotTradeHistory.entry_time <= end)
            .all()
        )
        if not trades:
            trades = (
                session.query(TradePosition)
                .filter(TradePosition.user_id == user_id)
                .filter(TradePosition.symbol == symbol.upper())
                .filter(TradePosition.created_at >= start)
                .filter(TradePosition.created_at <= end)
                .all()
            )
    finally:
        session.close()

    # Unique filename per interval + date (avoid image cache collisions)
    os.makedirs("app/static/plots", exist_ok=True)
    output_file = f"app/static/plots/{symbol}_{interval}_{date}.png"

    # Render + save
    plot(symbol, interval, df, k_series, d_series, k_smooth, d_smooth, trades, output_file)

    # Cache buster so browser always fetches fresh file
    v = int(time.time())
    img_url = f"/static/plots/{symbol}_{interval}_{date}.png?v={v}"

    html = f"""
    <html>
      <head><title>Trades for {symbol} on {date}</title></head>
      <body>
        <h2>{symbol} {interval} trades on {date}</h2>
        <img src="{img_url}" style="max-width:100%;">
      </body>
    </html>
    """
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
