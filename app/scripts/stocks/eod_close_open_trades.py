#!/usr/bin/env python3
"""
Close all (or scoped) open stock paper trades for bots that have eod_auto_close = TRUE.

Typical runs:
    # Dry-run: see which trades would close
    python -m app.scripts.stocks.eod_close_open_trades --dry-run --price-source entry

    # Actual close (uses live Schwab prices)
    python -m app.scripts.stocks.eod_close_open_trades --price-source live

    # Limit to one user or symbol
    python -m app.scripts.stocks.eod_close_open_trades --user-id 116 --symbol TSLA
"""

import argparse
import logging
from decimal import Decimal
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from app.utils.time_utils import get_now_et
from app.database.connection import SessionLocal
from app.models.user import User
from app.models.paper_trading import PaperAccount
from app.models.paper_trading_bot import (
    PaperStockTradeBot,
    PaperStockBotOpenTrade,
    PaperStockBotTradeHistory,
)
from app.utils.stock.market_price import get_live_price

log = logging.getLogger("eod_close_open_trades")


# ---------------------------
# Helpers
# ---------------------------
def _D(x) -> Decimal:
    """Convert to Decimal safely."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _resolve_exit_price(trade: PaperStockBotOpenTrade,
                        src: str,
                        fixed: Optional[float]) -> Optional[Decimal]:
    """Determine exit price based on --price-source."""
    src = (src or "live").lower()
    if src == "live":
        px = get_live_price(trade.symbol)
        if px is None:
            log.warning(f"[WARN] Live price unavailable for {trade.symbol}, fallback to entry.")
            return _D(trade.entry_price)
        return _D(px)
    elif src == "entry":
        return _D(trade.entry_price)
    elif src == "fixed":
        if fixed is None:
            raise ValueError("--price-source fixed requires --fixed-price")
        return _D(fixed)
    else:
        raise ValueError(f"Unknown --price-source {src}")


def _close_one(db: Session,
               ot: PaperStockBotOpenTrade,
               exit_price: Decimal,
               algo_name: str,
               note: Optional[str]) -> float:
    """Close one open trade, add history, update balance."""
    side = (ot.position_side or "").lower()
    qty = _D(ot.quantity)
    entry = _D(ot.entry_price)
    pnl = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty

    acct = db.query(PaperAccount).filter_by(user_id=ot.user_id).with_for_update().one_or_none()
    if acct is None:
        raise RuntimeError(f"No PaperAccount for user_id={ot.user_id}")

    acct.current_balance = float(_D(acct.current_balance) + pnl)

    hist = PaperStockBotTradeHistory(
        bot_id=ot.bot_id,
        user_id=ot.user_id,
        symbol=ot.symbol,
        trade_type="SELL" if side == "long" else "BUY",
        position_side=ot.position_side,
        quantity=ot.quantity,
        entry_price=ot.entry_price,
        entry_time=ot.entry_time,
        exit_price=float(exit_price),
        exit_time=get_now_et(),
        profit_loss=float(pnl),
        note=(note[:255] if note else None),
        algo_name=algo_name or "AUTO-EOD",
    )

    db.add(hist)
    db.delete(ot)
    return float(pnl)


# ---------------------------
# Main script
# ---------------------------
def main():
    ap = argparse.ArgumentParser(description="End-of-Day closer for stock paper bots.")
    ap.add_argument("--user-id", type=int, help="Only close trades for this user ID.")
    ap.add_argument("--bot-id", type=int, help="Only close trades for this bot ID.")
    ap.add_argument("--symbol", type=str, help="Only close this symbol.")
    ap.add_argument("--price-source", choices=["live", "entry", "fixed"], default="live")
    ap.add_argument("--fixed-price", type=float, help="Used only if --price-source=fixed")
    ap.add_argument("--algo-name", default="AUTO-EOD")
    ap.add_argument("--note", default="[AUTO-EOD CLOSE]")
    ap.add_argument("--ignore-eod-flag", action="store_true",
                    help="Ignore bot eod_auto_close flag (emergency use only).")
    ap.add_argument("--dry-run", action="store_true", help="Simulate without committing.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db: Session = SessionLocal()
    try:
        # ---------------------------
        # Query open trades
        # ---------------------------
        q = (
            db.query(PaperStockBotOpenTrade)
            .join(PaperStockTradeBot, PaperStockBotOpenTrade.bot_id == PaperStockTradeBot.id)
        )

        if not args.ignore_eod_flag:
            q = q.filter(PaperStockTradeBot.eod_auto_close.is_(True))

        if args.user_id:
            q = q.filter(PaperStockBotOpenTrade.user_id == args.user_id)
        if args.bot_id:
            q = q.filter(PaperStockBotOpenTrade.bot_id == args.bot_id)
        if args.symbol:
            q = q.filter(PaperStockBotOpenTrade.symbol == args.symbol)

        open_trades = q.all()
        if not open_trades:
            log.info("No open trades found matching criteria.")
            return 0

        log.info("Found %d open trade(s) to close.", len(open_trades))

        total_closed = 0
        total_pnl = 0.0

        for ot in open_trades:
            exit_px = _resolve_exit_price(ot, args.price_source, args.fixed_price)
            if exit_px is None:
                log.warning(f"[WARN] Could not resolve price for {ot.symbol}, skipping.")
                continue

            side = (ot.position_side or "").lower()
            trade_type = "SELL" if side == "long" else "BUY"
            pnl_preview = (
                float((exit_px - _D(ot.entry_price)) * _D(ot.quantity))
                if side == "long"
                else float((_D(ot.entry_price) - exit_px) * _D(ot.quantity))
            )

            if args.dry_run:
                log.info("[DRY] Would close %s (user=%s bot=%s) %s %s @ %.2f P/L=%.2f",
                         ot.symbol, ot.user_id, ot.bot_id,
                         trade_type, ot.quantity, float(exit_px), pnl_preview)
                total_closed += 1
                total_pnl += pnl_preview
                continue

            pnl = _close_one(db, ot, exit_px, args.algo_name, args.note)
            log.info("Closed %s (user=%s bot=%s) %s %s @ %.2f P/L=%.2f",
                     ot.symbol, ot.user_id, ot.bot_id,
                     trade_type, ot.quantity, float(exit_px), pnl)
            total_closed += 1
            total_pnl += pnl

        if args.dry_run:
            log.info("[DRY] Would close %d trade(s) total P/L=%.2f", total_closed, total_pnl)
            db.rollback()
        else:
            db.commit()
            log.info("✅ Closed %d trade(s). Total realized P/L=%.2f", total_closed, total_pnl)

        return 0

    except Exception as e:
        db.rollback()
        log.exception("❌ Error closing trades: %s", e)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
