#/var/www/stockwicks/app/routes/paper_trading.py
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime
from decimal import Decimal
from io import StringIO
import csv
import requests  # ✅ needed for live price fetch

from app.models.paper_trading import PaperAccount, PaperTrade, PaperOrder
from app.database.connection import get_db
from app.routes.auth import get_current_user
from app.models.user import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ✅ Fetch live market price from Schwab API
SCHWAB_API_URL = "http://localhost:8512/auth/schwab/quote"


@router.get("/auth/papertrading", response_class=HTMLResponse)
def paper_trading_dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    print(f"[DEBUG] Loading dashboard for {user.email} (user_id={user.id})")

    db.expire_all()  # refresh session

    # ✅ Fetch ALL orders for debug
    all_orders = db.query(PaperOrder).filter(PaperOrder.user_id == user.id).all()
    print(f"[DEBUG] Retrieved ALL {len(all_orders)} orders for {user.email}")
    for o in all_orders:
        print(f"  -> ORDER ID={o.id}, {o.symbol}, status={o.status}, qty={o.quantity}, limit={o.limit_price}")

    # ✅ Ensure paper account exists
    paper_account = db.query(PaperAccount).filter(PaperAccount.user_id == user.id).first()
    if not paper_account:
        paper_account = PaperAccount(user_id=user.id, current_balance=100000.0)
        db.add(paper_account)
        db.commit()
        db.refresh(paper_account)
        print(f"[DEBUG] Created new paper account for {user.email} with $100,000 balance")

    # ✅ FIX: Case-insensitive filter for pending orders
    from sqlalchemy import func
    pending_orders = db.query(PaperOrder).filter(
        PaperOrder.user_id == user.id,
        func.upper(PaperOrder.status) == "WORKING"
    ).all()

    print(f"[DEBUG] Retrieved {len(pending_orders)} pending_orders for user {user.email}")
    for o in pending_orders:
        print(f"  -> PENDING ORDER {o.id} {o.symbol} side={o.side} qty={o.quantity} limit={o.limit_price} status={o.status}")

    # ✅ Fetch open & closed trades
    open_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user.id,
        PaperTrade.status == "OPEN"
    ).all()

    closed_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user.id,
        PaperTrade.status == "CLOSED"
    ).all()

    return templates.TemplateResponse("paper_trading.html", {
        "request": request,
        "user": user,
        "paper_account": paper_account,
        "pending_orders": pending_orders,
        "open_trades": open_trades,
        "closed_trades": closed_trades
    })

@router.post("/auth/papertrading/cancel-order/{order_id}")
def cancel_pending_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    order = db.query(PaperOrder).filter_by(id=order_id, user_id=user.id, status="WORKING").first()
    if not order:
        print(f"[ERROR] Cancel failed: Order {order_id} not found for {user.email}")
        raise HTTPException(status_code=404, detail="Order not found or already filled/cancelled")

    order.status = "CANCELLED"
    order.cancelled_at = datetime.utcnow()

    db.commit()
    print(f"[DEBUG] Order {order_id} cancelled for {user.email}")

    return RedirectResponse(url="/auth/papertrading", status_code=302)
@router.post("/auth/papertrading/place-trade")
def place_paper_trade(
    request: Request,
    symbol: str = Form(...),
    trade_type: str = Form(...),  # BUY/SELL
    quantity: float = Form(...),
    entry_price: float = Form(...),
    profit_target: float = Form(None),
    stop_loss: float = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    print("[DEBUG] FORM DATA RECEIVED:")
    print("symbol:", symbol)
    print("trade_type:", trade_type)
    print("quantity:", quantity)
    print("entry_price:", entry_price)
    print("profit_target:", profit_target)
    print("stop_loss:", stop_loss)

    paper_account = db.query(PaperAccount).filter_by(user_id=user.id).first()
    if not paper_account:
        raise HTTPException(status_code=400, detail="Paper account not found!")

    # ✅ Always store uppercase WORKING for consistency
    new_order = PaperOrder(
        user_id=user.id,
        symbol=symbol.upper(),
        order_type="limit",
        side=trade_type.lower(),
        position_side="long",
        limit_price=entry_price,
        quantity=quantity,
        profit_target=profit_target,
        stop_loss=stop_loss,
        status="WORKING",
        created_at=datetime.utcnow()
    )

    db.add(new_order)

    try:
        # ✅ Flush + commit so it’s immediately visible
        db.flush()
        db.commit()
        db.refresh(new_order)
        print(f"[DEBUG] ✅ New PaperOrder saved → ID={new_order.id}, Status={new_order.status}")
    except Exception as e:
        db.rollback()
        print(f"[ERROR] ❌ Failed to save PaperOrder: {e}")
        return {"error": str(e)}

    # ✅ Confirm it’s visible in DB after commit
    check_orders = db.query(PaperOrder).filter_by(user_id=user.id).all()
    print(f"[DEBUG] DB Now Has {len(check_orders)} orders for user_id={user.id}")

    return RedirectResponse(url="/auth/papertrading", status_code=302)


@router.post("/auth/papertrading/close-trade/{trade_id}")
def close_trade(
    trade_id: int,
    price: float = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    trade = db.query(PaperTrade).filter_by(id=trade_id, user_id=user.id, status="OPEN").first()
    if not trade:
        print(f"[ERROR] Trade {trade_id} not found or already closed for user {user.email}")
        return {"error": "Trade not found!"}

    print(f"[DEBUG] Closing trade {trade.symbol} for {user.email} @ exit price {price}")

    # Set exit price & close trade
    trade.exit_price = price
    trade.status = "CLOSED"
    trade.closed_at = datetime.utcnow()

    account = db.query(PaperAccount).filter_by(user_id=user.id).first()

    # Calculate total cash movement on exit
    exit_total = Decimal(str(price)) * Decimal(str(trade.quantity))
    entry_total = Decimal(str(trade.entry_price)) * Decimal(str(trade.quantity))

    # Update current balance depending on trade type
    if trade.trade_type == "BUY":
        # On closing a long position, add back the exit value (buy low, sell high = profit)
        account.current_balance += exit_total
    else:
        # On closing a short position, pay back the shares (sell high, buy low = profit)
        account.current_balance -= exit_total

    db.commit()
    print(f"[DEBUG] Closed trade {trade.symbol} for user {user.email} | Final balance={account.current_balance}")

    return RedirectResponse(url="/auth/papertrading", status_code=302)


@router.post("/auth/papertrading/reset-balance")
def reset_paper_balance(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    account = db.query(PaperAccount).filter_by(user_id=user.id).first()
    if account:
        account.current_balance = 100000.0
        db.query(PaperTrade).filter_by(user_id=user.id).delete()
        db.query(PaperOrder).filter_by(user_id=user.id).delete()  # ✅ Clear all pending orders too
        db.commit()
        print(f"[DEBUG] Reset paper account for user {user.email}")

    return RedirectResponse(url="/auth/papertrading", status_code=302)


@router.get("/auth/papertrading/download-csv")
def download_trade_history(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    trades = (
        db.query(PaperTrade)
        .filter_by(user_id=user.id)
        .order_by(PaperTrade.closed_at.desc().nullslast())
        .all()
    )

    output = StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Symbol",
        "Type",
        "Quantity",
        "Entry Price",
        "Exit Price",
        "Profit/Loss",
        "Entry Time",
        "Exit Time",
        "Status"
    ])

    for t in trades:
        pl = None
        if t.exit_price is not None:
            pl = (t.exit_price - t.entry_price) * t.quantity if t.trade_type == "BUY" else (t.entry_price - t.exit_price) * t.quantity
        
        entry_time = t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else ""
        exit_time = t.closed_at.strftime("%Y-%m-%d %H:%M:%S") if t.closed_at else ""

        writer.writerow([
            t.symbol,
            t.trade_type,
            t.quantity,
            t.entry_price,
            t.exit_price if t.exit_price else "",
            round(pl, 2) if pl is not None else "",
            entry_time,
            exit_time,
            t.status
        ])

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_history.csv"}
    )


def check_and_auto_close_trades(db: Session, user_id: int, get_price_func):
    open_trades = db.query(PaperTrade).filter_by(user_id=user_id, status="OPEN").all()
    closed_count = 0

    for trade in open_trades:
        current_price = get_price_func(trade.symbol)
        if not current_price:
            continue  # if API fails, skip

        if trade.trade_type == "BUY":
            if trade.profit_target and current_price >= trade.profit_target:
                print(f"[AUTO] Closing {trade.symbol} (BUY) at profit target {trade.profit_target}")
                close_trade_logic(db, trade, current_price)
                closed_count += 1
            elif trade.stop_loss and current_price <= trade.stop_loss:
                print(f"[AUTO] Closing {trade.symbol} (BUY) at stop loss {trade.stop_loss}")
                close_trade_logic(db, trade, current_price)
                closed_count += 1

        elif trade.trade_type == "SELL":
            if trade.profit_target and current_price <= trade.profit_target:
                print(f"[AUTO] Closing {trade.symbol} (SELL) at profit target {trade.profit_target}")
                close_trade_logic(db, trade, current_price)
                closed_count += 1
            elif trade.stop_loss and current_price >= trade.stop_loss:
                print(f"[AUTO] Closing {trade.symbol} (SELL) at stop loss {trade.stop_loss}")
                close_trade_logic(db, trade, current_price)
                closed_count += 1

    if closed_count > 0:
        db.commit()
        print(f"[AUTO] Auto-closed {closed_count} trades")


def close_trade_logic(db: Session, trade: PaperTrade, exit_price: float):
    trade.exit_price = exit_price
    trade.status = "CLOSED"
    trade.closed_at = datetime.utcnow()

    account = db.query(PaperAccount).filter_by(user_id=trade.user_id).first()
    trade_cost = Decimal(str(trade.quantity)) * Decimal(str(exit_price))

    if trade.trade_type == "BUY":
        account.current_balance += trade_cost
    else:  # SELL trade
        account.current_balance -= trade_cost


def get_live_price(symbol: str):
    try:
        resp = requests.get(f"{SCHWAB_API_URL}?symbol={symbol}")
        if resp.status_code == 401:
            print(f"[ERROR] Schwab token expired. Please refresh with /auth/schwab/start")
            return None
        resp.raise_for_status()
        data = resp.json()

        if symbol in data and "quote" in data[symbol]:
            return data[symbol]["quote"].get("lastPrice")
        else:
            print(f"[ERROR] Unexpected Schwab API format: {data}")
            return None

    except Exception as e:
        print(f"[ERROR] Failed to fetch price for {symbol}: {e}")
        return None
