#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/options/option_bots_runner.py

"""
Options paper-bot runner (compatible with option_tasks.py)

FIX:
- manage_open_trades() now supports being called as:
    manage_open_trades(db=db, user_id=user_id)
  AND also:
    manage_open_trades(user_id=user_id)

This prevents Celery crash:
  TypeError: manage_open_trades() got an unexpected keyword argument 'db'

Keeps your existing architecture intact.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import asyncio
import inspect
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import pytz
import requests
import pandas as pd

# --- Project Setup (ensure /var/www/stockwicks is on sys.path) ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- Core Imports ---
from app.database.connection import SessionLocal
from app.models.paper_option_trading_bot import PaperOptionTradeBot, PaperOptionBotOpenTrade
from app.utils.stock.schwab_token import get_valid_access_token
from app.utils.logging_config import setup_bot_logger

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] (OPTION_RUNNER) %(message)s")

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
EASTERN = pytz.timezone("US/Eastern")

# =========================================================================
# Exit / risk configuration
# =========================================================================
FEE_ROUND_TRIP_PER_CONTRACT = float(os.getenv("OPTION_FEE_ROUND_TRIP", "1.50"))

MIN_NET_PROFIT_USD = float(os.getenv("OPTION_MIN_NET_PROFIT", "15.0"))

# Default loss caps (you can tune)
MAX_NET_LOSS_USD = float(os.getenv("OPTION_MAX_NET_LOSS", "-30.0"))

TIME_STOP_MINUTES = int(os.getenv("OPTION_TIME_STOP_MINUTES", "45"))
TIME_STOP_MIN_NET_USD = float(os.getenv("OPTION_TIME_STOP_MIN_NET", "3.0"))
GIVEBACK_FLOOR_USD = float(os.getenv("OPTION_GIVEBACK_FLOOR", "5.0"))

# Entry filters
SCORE_THRESH_CREDIT = float(os.getenv("OPTION_SCORE_THRESH_CREDIT", "72"))
SCORE_THRESH_DEBIT = float(os.getenv("OPTION_SCORE_THRESH_DEBIT", "78"))

# -------------------------------------------------------------------
def _jdump(obj: Any, maxlen: int = 2000) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) > maxlen:
        return s[:maxlen] + f"... [truncated {len(s)-maxlen} chars]"
    return s


def _bot_style(bot: PaperOptionTradeBot) -> str:
    raw = getattr(bot, "algo_params", None)
    try:
        if raw is None or raw == "":
            d = {}
        elif isinstance(raw, dict):
            d = raw
        elif isinstance(raw, str):
            d = json.loads(raw) if raw.strip() else {}
        else:
            d = dict(raw)
        return (d.get("style") or "credit").strip().lower()
    except Exception:
        return "credit"


def _get_schwab_headers() -> Dict[str, str]:
    access_token = get_valid_access_token()
    if not access_token:
        raise ValueError("Could not get Schwab access token.")
    return {"Authorization": f"Bearer {access_token}"}


def _get_schwab_intraday_data(symbol: str, logger: logging.Logger = log) -> Optional[pd.DataFrame]:
    params = {
        "symbol": symbol.upper(),
        "periodType": "day",
        "period": 1,
        "frequencyType": "minute",
        "frequency": 1,
        "needExtendedHoursData": "false",
    }
    url = f"{SCHWAB_API_URL}/pricehistory"
    try:
        resp = requests.get(url, headers=_get_schwab_headers(), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data or "candles" not in data:
            return None
        df = pd.DataFrame(data["candles"])
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
            df.set_index("datetime", inplace=True)
        return df
    except Exception as e:
        logger.error(f"Failed to fetch intraday data for {symbol}: {e}", exc_info=True)
        return None


def _fetch_and_process_chain(symbol: str, days_out: int = 45, logger: logging.Logger = log) -> tuple[list, float | None]:
    url = f"{SCHWAB_API_URL}/chains"
    today_str = datetime.now().strftime("%Y-%m-%d")
    future_date_str = (datetime.now() + timedelta(days=days_out)).strftime("%Y-%m-%d")

    params = {
        "symbol": symbol,
        "fromDate": today_str,
        "toDate": future_date_str,
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "range": "ALL",
    }
    try:
        resp = requests.get(url, headers=_get_schwab_headers(), params=params, timeout=15)
        logger.info(f"Option Chain API [{symbol}] status: {resp.status_code}")
        if resp.status_code != 200:
            return [], None

        chain = resp.json()
        price = chain.get("underlying", {}).get("last")
        if not price:
            uq = chain.get("underlyingQuote") or {}
            price = uq.get("lastPrice") or uq.get("askPrice") or uq.get("bidPrice")

        options: list[dict] = []
        required = ["symbol", "strikePrice", "bid", "ask", "volatility", "delta", "openInterest"]

        for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
            for exp_key, strikes in (chain.get(key, {}) or {}).items():
                exp = exp_key.split(":")[0]
                for _, contracts in (strikes or {}).items():
                    for c in contracts:
                        if all(k in c and c[k] is not None for k in required):
                            options.append(
                                {
                                    "putCall": side.upper(),
                                    "symbol": c["symbol"].replace(" ", ""),
                                    "strike": float(c["strikePrice"]),
                                    "bid": float(c["bid"]),
                                    "ask": float(c["ask"]),
                                    "volatility": float(c["volatility"]),
                                    "delta": float(c["delta"]),
                                    "openInterest": int(c["openInterest"]),
                                    "mark": float(c.get("mark", c["bid"])),
                                    "expiration": exp,
                                    "occ": c["symbol"].replace(" ", ""),
                                }
                            )
        return options, float(price) if price else None
    except Exception as e:
        logger.error(f"_fetch_and_process_chain error for {symbol}: {e}", exc_info=True)
        return [], None


# =========================================================================
# Algo dispatcher
# =========================================================================
def run_guru_pick_4exp_bots(db, bots: List[PaperOptionTradeBot], df, chain, underlying_price, logger):
    from app.scripts.options.algos.guru_pick_4exp import run_guru_pick_4exp_bots as guru_pick_4exp_bot_handler

    for bot in bots:
        try:
            raw = getattr(bot, "algo_params", None)
            if raw is None or raw == "":
                algo_params = {}
            elif isinstance(raw, dict):
                algo_params = raw
            elif isinstance(raw, str):
                algo_params = json.loads(raw) if raw.strip() else {}
            else:
                try:
                    algo_params = dict(raw)
                except Exception:
                    algo_params = {}

            bot_params: Dict[str, Any] = {"symbol": bot.symbol}
            bot_params["strategy"] = algo_params.get("strategy", "auto")
            bot_params["style"] = algo_params.get("style", "credit")
            bot_params["expires"] = int(algo_params.get("expires", 4))
            bot_params["top"] = int(algo_params.get("top", 8))

            if algo_params.get("min_premium") is not None:
                bot_params["min_premium"] = float(algo_params["min_premium"])
            if algo_params.get("max_premium") is not None:
                bot_params["max_premium"] = float(algo_params["max_premium"])
            if algo_params.get("max_allocation_usd") is not None:
                bot_params["max_allocation_usd"] = float(algo_params["max_allocation_usd"])
            if algo_params.get("contracts") is not None:
                bot_params["contracts"] = int(algo_params["contracts"])

            logger.info(f"[GURU_CALL] bot {bot.id} params={_jdump(bot_params)}")

            maybe = guru_pick_4exp_bot_handler(bot_params)
            result = asyncio.run(maybe) if inspect.isawaitable(maybe) else maybe

            if not isinstance(result, dict):
                logger.error(f"[GURU_RESULT] bad result type={type(result)} value={result}")
                continue

            if result.get("success", False):
                _process_guru_results(db, bot, result, logger)
            else:
                logger.error(f"[GURU_RESULT] scan failed: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logger.error(f"Error processing bot {getattr(bot, 'id', 'N/A')}: {e}", exc_info=True)
            continue


ALGO_DISPATCHER = {"guru_pick_4exp": run_guru_pick_4exp_bots}

def _process_guru_results(db, bot, result: Dict[str, Any], logger):
    """
    Create a PaperOptionBotOpenTrade from guru_pick_4exp result.

    Robust to different result formats:
      - best trade key may be: best_overall_trade / best_trade / best_candidate
      - score key may be: score / quality_score / final_score
    """

    # ---- locate best trade dict ----
    best_trade = (
        result.get("best_overall_trade")
        or result.get("best_trade")
        or result.get("best_candidate")
    )
    if not isinstance(best_trade, dict):
        logger.info("[CREATE] No best trade found in result.")
        return

    # ---- extract score robustly ----
    score_raw = None
    for k in ("score", "quality_score", "final_score"):
        v = best_trade.get(k)
        if v is not None and v != "":
            score_raw = v
            break

    try:
        score = float(score_raw) if score_raw is not None else 0.0
    except Exception:
        score = 0.0

    # If score is missing/0, log keys once to diagnose schema mismatch
    if score <= 0:
        logger.warning(
            f"[CREATE] best_trade missing score or score<=0. "
            f"keys={list(best_trade.keys())} best_trade={_jdump(best_trade, 1200)}"
        )
        return

    style = _bot_style(bot)
    thresh = SCORE_THRESH_DEBIT if style == "debit" else SCORE_THRESH_CREDIT
    if score < thresh:
        logger.info(f"[CREATE] below score threshold: {score:.2f} < {thresh:.2f}")
        return

    # ---- skip if already has open trade ----
    open_cnt = (
        db.query(PaperOptionBotOpenTrade)
        .filter(
            PaperOptionBotOpenTrade.bot_id == bot.id,
            PaperOptionBotOpenTrade.status == "OPEN",
        )
        .count()
    )
    if open_cnt >= 1:
        logger.info(f"[CREATE] bot {bot.id} already has open trade; skipping.")
        return

    # ---- required fields (with safer parsing) ----
    opt_symbol = (best_trade.get("symbol") or best_trade.get("occ") or "").replace(" ", "").strip()
    if not opt_symbol:
        logger.warning(f"[CREATE] missing option symbol in best_trade={_jdump(best_trade, 800)}")
        return

    action = str(best_trade.get("action", "") or best_trade.get("side", "")).upper()
    side = "CALL" if "CALL" in action else "PUT"
    pos_side = "sell" if action.startswith("SELL") or action == "SELL" else "buy"

    def _f(key: str, default: float | None = None) -> float | None:
        v = best_trade.get(key)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except Exception:
            return default

    entry_price = _f("entry_price")
    stop_price = _f("stop_price")
    t1 = _f("target_1") or _f("target1") or _f("take_profit")  # tolerate alt keys
    strike = _f("strike")

    if entry_price is None or stop_price is None or t1 is None or strike is None:
        logger.warning(
            "[CREATE] missing required numeric fields "
            f"(entry={entry_price}, stop={stop_price}, t1={t1}, strike={strike}). "
            f"best_trade={_jdump(best_trade, 1200)}"
        )
        return

    exp_str = str(best_trade.get("expiration", "") or best_trade.get("expiry", "")).strip()
    if not exp_str:
        logger.warning(f"[CREATE] missing expiration in best_trade={_jdump(best_trade, 800)}")
        return
    try:
        expiry_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
    except Exception:
        logger.warning(f"[CREATE] bad expiration format '{exp_str}' in best_trade={_jdump(best_trade, 800)}")
        return

    try:
        qty = int(best_trade.get("position_size", 1) or 1)
    except Exception:
        qty = 1
    if qty < 1:
        qty = 1

    # ---- create trade ----
    new_trade = PaperOptionBotOpenTrade(
        user_id=bot.user_id,
        bot_id=bot.id,
        underlying_symbol=bot.symbol,
        option_symbol=opt_symbol,
        position_side=pos_side,
        quantity=qty,
        entry_price=float(entry_price),
        strike_price=float(strike),
        side=side,
        expiry_date=expiry_date,
        status="OPEN",
        planned_stop_loss=float(stop_price),
        planned_take_profit=float(t1),
        entry_time=datetime.now(),
    )

    db.add(new_trade)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[CREATE] DB commit failed: {e}", exc_info=True)
        return

    logger.info(
        f"[CREATE] ✅ Created trade id={new_trade.id} "
        f"{opt_symbol} {pos_side} qty={qty} entry={float(entry_price):.2f} score={score:.2f}"
    )


# =========================================================================
# MAIN BOT RUNNER
# =========================================================================


def run_option_bots_tick(user_id: Optional[int] = None):
    # ---- PHASE 1: read bot list quickly, then close session ----
    db = SessionLocal()
    try:
        q = db.query(PaperOptionTradeBot).filter(PaperOptionTradeBot.is_active.is_(True))
        if user_id:
            q = q.filter(PaperOptionTradeBot.user_id == user_id)
        active_bots = q.all()
    finally:
        try:
            db.close()
        except Exception:
            pass

    if not active_bots:
        return {"status": "NO_ACTIVE_BOTS", "total_bots": 0}

    # ---- PHASE 2: do slow network work WITHOUT db session open ----
    for bot in active_bots:
        logger = setup_bot_logger(user_id=bot.user_id, bot_id=bot.id)
        if bot.algo_name not in ALGO_DISPATCHER:
            continue

        df_1min = _get_schwab_intraday_data(bot.symbol, logger=logger)
        if df_1min is None or df_1min.empty:
            continue

        chain, underlying_price = _fetch_and_process_chain(bot.symbol, logger=logger)
        if not chain:
            continue

        # ---- PHASE 3: open db session only for writing ----
        db2 = SessionLocal()
        try:
            handler = ALGO_DISPATCHER[bot.algo_name]
            handler(db=db2, bots=[bot], df=df_1min, chain=chain, underlying_price=underlying_price, logger=logger)
            db2.commit()
        except Exception:
            db2.rollback()
            raise
        finally:
            try:
                db2.close()
            except Exception:
                pass

    return {"status": "OK", "total_bots": len(active_bots)}

# =========================================================================
# ✅ FIXED SIGNATURE: manage_open_trades(db=..., user_id=...) COMPATIBLE
# =========================================================================
def manage_open_trades(
    user_id: Optional[int] = None,
    db=None,
    **kwargs,
):
    """
    Backward-compatible signature:
      manage_open_trades(db=db, user_id=user_id)

    This is required by:
      app/tasks/option_tasks.py -> manage_all_open_trades()
    """
    from app.utils.options.options_pricing import mark_open_trade
    from app.utils.options.option_trade_utils import close_option_trade

    min_net_profit = float(kwargs.get("min_net_profit_usd", MIN_NET_PROFIT_USD))
    max_net_loss = float(kwargs.get("max_net_loss_usd", MAX_NET_LOSS_USD))

    _owned_db = False
    if db is None:
        db = SessionLocal()
        _owned_db = True

    try:
        q = db.query(PaperOptionBotOpenTrade).filter(PaperOptionBotOpenTrade.status == "OPEN")
        if user_id:
            q = q.filter(PaperOptionBotOpenTrade.user_id == user_id)

        open_trades = q.all()
        if not open_trades:
            return {"processed": 0, "closed": 0}

        processed = 0
        closed = 0

        for t in open_trades:
            processed += 1
            logger = setup_bot_logger(user_id=t.user_id, bot_id=t.bot_id)

            # mark pricing – tolerate different signatures
            mark = None
            for attempt in (
                lambda: mark_open_trade(t, logger=logger),
                lambda: mark_open_trade(open_trade=t, logger=logger),
                lambda: mark_open_trade(t),
                lambda: mark_open_trade(open_trade=t),
                lambda: mark_open_trade(t, db=db),
                lambda: mark_open_trade(open_trade=t, db=db),
            ):
                try:
                    mark = attempt()
                    break
                except TypeError:
                    continue

            if mark is None:
                continue

            try:
                mark_f = float(mark)
                if mark_f <= 0:
                    continue
            except Exception:
                continue

            entry = float(getattr(t, "entry_price", 0) or 0)
            qty = int(getattr(t, "quantity", 1) or 1)
            pos_side = (getattr(t, "position_side", "") or "").strip().lower()

            if pos_side == "buy":
                gross = (mark_f - entry) * 100.0 * qty
            else:
                gross = (entry - mark_f) * 100.0 * qty

            net = gross - (FEE_ROUND_TRIP_PER_CONTRACT * qty)

            # persist
            try:
                t.current_mark_price = round(mark_f, 4)
                t.unrealized_pl = round(gross, 2)
                if hasattr(t, "unrealized_pl_net"):
                    setattr(t, "unrealized_pl_net", round(net, 2))
                db.commit()
            except Exception:
                db.rollback()

            should_close = False
            reason = None

            if net >= min_net_profit:
                should_close = True
                reason = "NET_TP"
            elif net <= max_net_loss:
                should_close = True
                reason = "NET_SL"

            if not should_close:
                continue

            try:
                # close using utility
                close_option_trade(t, mark_f)
                closed += 1
                logger.info(f"[manage_open_trades] ✅ closed trade_id={t.id} net={net:.2f} reason={reason}")
            except Exception as e:
                logger.error(f"[manage_open_trades] close failed trade_id={t.id} err={e}", exc_info=True)
                db.rollback()

        return {"processed": processed, "closed": closed}

    finally:
        if _owned_db:
            db.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--task", choices=["find_trades", "manage_trades"], required=True)
    p.add_argument("--user_id", type=int)
    args = p.parse_args()

    if args.task == "find_trades":
        print(run_option_bots_tick(user_id=args.user_id))
    else:
        print(manage_open_trades(user_id=args.user_id))
