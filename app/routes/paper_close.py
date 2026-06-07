from fastapi import APIRouter, Depends, HTTPException
from app.database.connection import SessionLocal
from app.routes.auth import get_current_user  # <-- adjust to your auth helper path
from app.utils.stock.paper_trade_engine import close_open_trade_at_market, close_all_open_trades_for_bot

router = APIRouter(prefix="/api/paper", tags=["paper"])

@router.post("/close-trade/{open_trade_id}")
def api_close_trade(open_trade_id: int, user=Depends(get_current_user)):
    ok = close_open_trade_at_market(user_id=user.id, open_trade_id=open_trade_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Unable to close trade")
    return {"ok": True}

@router.post("/close-all/{bot_id}")
def api_close_all(bot_id: int, user=Depends(get_current_user)):
    n = close_all_open_trades_for_bot(user_id=user.id, bot_id=bot_id)
    return {"ok": True, "closed_count": n}
