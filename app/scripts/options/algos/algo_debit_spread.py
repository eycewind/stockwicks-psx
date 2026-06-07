# /var/www/stockwicks/app/scripts/options/algos/algo_debit_spread.py
import logging
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
import pandas as pd
import pandas_ta as ta  # ensure installed
from app.models.paper_option_trading_bot import PaperOptionTradeBot, PaperOptionBotOpenTrade
from app.utils.options.option_trade_utils import open_spread_trade
from app.utils.options.expiry_guard import filter_expiries


# ---------------- small helpers (local to this algo) ----------------
def _jdump(obj: Any, maxlen: int = 2000) -> str:
    """Safe JSON-ish dump shortened to avoid log bloat."""
    try:
        import json
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) > maxlen:
        return s[:maxlen] + f"... [truncated {len(s)-maxlen} chars]"
    return s


def _coerce_float(x, default=None) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return default


def _log_calc(logger: logging.Logger, name: str, payload: Dict[str, Any]):
    logger.info("[CALC] %s -> %s", name, _jdump(payload))


def _log_decision(logger: logging.Logger, decision: str, reason: str, payload: Dict[str, Any] | None = None):
    logger.info("[DECISION] %s | reason=%s | %s", decision, reason, _jdump(payload or {}))


def _to_date(exp_str: str) -> Optional[date]:
    try:
        return datetime.strptime(exp_str, "%Y-%m-%d").date()
    except Exception:
        return None


# ---------------- main algo ----------------
def run_debit_spread_bots(
    db: Session,
    bots: List[PaperOptionTradeBot],
    df: pd.DataFrame,
    chain: list,
    underlying_price: float,
    **kwargs,
):
    """
    debit_spread_v1:
      - Uses MACD(12,26,9) crossover on underlying.
      - If MACD crosses UP  -> look for CALL debit spread (buy lower strike, sell higher strike).
      - If MACD crosses DOWN-> look for PUT  debit spread (buy higher strike, sell lower strike).
      - Picks expiry from `filter_expiries` (e.g., exclude too-near expirations).
      - Prices spread as ASK(long) - BID(short); checks debit bounds before entry.
    """
    logger: logging.Logger = kwargs.get("logger", logging.getLogger("option_algo_debit"))

    # ---------- Guards ----------
    u = _coerce_float(underlying_price, default=None)
    if u is None:
        logger.warning("[DEBIT SPREAD] No numeric underlying price; cannot run.")
        return
    if df is None or df.empty:
        logger.warning("[DEBIT SPREAD] Missing/empty minute bars; cannot run.")
        return
    for col in ("close", "high", "low"):
        if col not in df.columns:
            logger.error("[DEBIT SPREAD] '%s' column missing; cannot compute MACD.", col)
            return
    if not chain:
        logger.info("[DEBIT SPREAD] Empty chain; nothing to evaluate.")
        return

    # ---------- MACD 12/26/9 ----------
    try:
        ta.macd(df["close"], fast=12, slow=26, signal=9, append=True)
        macd_col = next((c for c in df.columns if c.startswith("MACD_12_26_9") and "MACDs" not in c and "MACDh" not in c), None)
        sig_col  = next((c for c in df.columns if c.startswith("MACDs_12_26_9")), None)
        if not macd_col or not sig_col:
            logger.error("[DEBIT SPREAD] MACD columns not found after calculation.")
            return

        macd = df[macd_col]
        sig  = df[sig_col]
        if macd.empty or sig.empty or len(macd) < 2:
            logger.info("[DEBIT SPREAD] Not enough data for MACD crossover check.")
            return

        cross_up   = (macd.iloc[-2] < sig.iloc[-2]) and (macd.iloc[-1] > sig.iloc[-1])
        cross_down = (macd.iloc[-2] > sig.iloc[-2]) and (macd.iloc[-1] < sig.iloc[-1])
        side = "call" if cross_up else ("put" if cross_down else None)
        _log_calc(logger, "macd", {
            "macd_prev": _coerce_float(macd.iloc[-2]),
            "sig_prev":  _coerce_float(sig.iloc[-2]),
            "macd_last": _coerce_float(macd.iloc[-1]),
            "sig_last":  _coerce_float(sig.iloc[-1]),
            "cross_up":  bool(cross_up),
            "cross_down":bool(cross_down),
            "side": side,
        })
    except Exception:
        logger.exception("[DEBIT SPREAD] MACD calculation failed")
        return

    if not side:
        _log_decision(logger, "NO-TRADE", "no MACD crossover", {})
        return

    # ---------- Sanitize chain + expiry filtering ----------
    sanitized: list[Dict[str, Any]] = []
    for c in chain:
        try:
            put_call = (c.get("putCall") or "").upper()
            strike   = _coerce_float(c.get("strike"), None)
            bid      = _coerce_float(c.get("bid"), None)
            ask      = _coerce_float(c.get("ask"), None)
            exp      = c.get("expiration")
            occ      = c.get("occ") or c.get("symbol")
            if put_call in ("CALL", "PUT") and strike is not None and bid is not None and ask is not None and exp and occ:
                sanitized.append({
                    "putCall": put_call, "strike": strike,
                    "bid": bid, "ask": ask, "expiration": exp, "occ": occ
                })
        except Exception:
            continue

    # candidate side filter
    side_u = side.upper()
    side_candidates = [c for c in sanitized if c["putCall"] == side_u]

    # expiry set -> filter via expiry_guard
    all_exp_dates = list({_to_date(c.get("expiration")) for c in side_candidates if c.get("expiration")})
    safe_exp_dates = filter_expiries([d for d in all_exp_dates if d], min_hours_to_expiry=12)
    safe_exp_strs = {d.strftime("%Y-%m-%d") for d in safe_exp_dates}

    # restrict to safe expiries
    side_candidates = [c for c in side_candidates if c["expiration"] in safe_exp_strs]

    _log_calc(logger, "chain_summary", {
        "total_chain": len(chain),
        "valid_sanitized": len(sanitized),
        f"{side_u}_candidates": len(side_candidates),
        "safe_expiries": sorted(list(safe_exp_strs))[:6],  # truncate for logs
    })

    if not side_candidates:
        _log_decision(logger, "NO-TRADE", f"no {side_u} candidates after expiry filtering", {})
        return

    # ---------- Helpers to choose legs ----------
    def pick_long_leg(cands: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Closest-to-ATM for the chosen side & safe expiries."""
        try:
            return min(cands, key=lambda c: abs(c["strike"] - u))
        except ValueError:
            return None

    def pick_short_leg(expiry: str, long_strike: float, width: float) -> Optional[Dict[str, Any]]:
        """Pick same-expiry short leg at long_strike ± width (direction depends on side)."""
        target = (long_strike + width) if side == "call" else (long_strike - width)
        same_exp = [c for c in side_candidates if c["expiration"] == expiry]
        if not same_exp:
            return None
        try:
            return min(same_exp, key=lambda c: abs(c["strike"] - target))
        except ValueError:
            return None

    # ---------- Iterate bots ----------
    for bot in bots:
        # 1) Skip if bot already has an open trade
        has_open = db.query(PaperOptionBotOpenTrade).filter_by(bot_id=bot.id).first() is not None
        if has_open:
            logger.info("[DEBIT SPREAD] Bot #%s: Open trade exists; skipping.", bot.id)
            continue

        params = bot.algo_params or {}
        width = _coerce_float(params.get("width"), 5.0) or 5.0
        if width <= 0:
            width = 5.0

        # 2) Long leg (ATM-ish)
        long_leg = pick_long_leg(side_candidates)
        if not long_leg:
            _log_decision(logger, "NO-TRADE", "no long leg (ATM) found", {})
            continue

        exp = long_leg["expiration"]
        short_leg = pick_short_leg(exp, long_leg["strike"], width)
        if not short_leg:
            _log_decision(logger, "NO-TRADE", "no short leg found at target width", {"expiry": exp, "width": width})
            continue

        # Ensure distinct strikes
        if abs(short_leg["strike"] - long_leg["strike"]) < 1e-6:
            _log_decision(logger, "NO-TRADE", "long/short identical strikes", {"strike": long_leg["strike"]})
            continue

        # 3) Debit pricing (ASK long − BID short)
        long_price  = _coerce_float(long_leg.get("ask"), 0.0) or 0.0
        short_price = _coerce_float(short_leg.get("bid"), 0.0) or 0.0
        debit_cost  = round(long_price - short_price, 2)
        width_eff   = abs(short_leg["strike"] - long_leg["strike"])

        # sanity: debit within [0.10, 0.60 * width]
        low_bound  = 0.10
        high_bound = round(width_eff * 0.60, 2)
        if debit_cost < low_bound or debit_cost > high_bound:
            _log_decision(
                logger, "NO-TRADE", "debit out of acceptable range",
                {"debit": debit_cost, "bounds": [low_bound, high_bound], "width_eff": width_eff}
            )
            continue

        _log_calc(logger, "spread_selection", {
            "side": side, "expiry": exp,
            "long": {"occ": long_leg["occ"], "strike": long_leg["strike"], "ask": long_price},
            "short":{"occ": short_leg["occ"], "strike": short_leg["strike"], "bid": short_price},
            "debit": debit_cost, "width_eff": width_eff
        })

        # 4) TP/SL as spread value levels (defaults 50/50)
        tp_pct = _coerce_float(params.get("tp_pct"), 50.0) or 50.0
        sl_pct = _coerce_float(params.get("sl_pct"), 50.0) or 50.0
        if tp_pct <= 0: tp_pct = 50.0
        if sl_pct <= 0: sl_pct = 50.0

        tp_value = round(debit_cost * (1 + tp_pct / 100.0), 2)
        sl_value = max(0.01, round(debit_cost * (1 - sl_pct / 100.0), 2))

        _log_calc(logger, "tp_sl_values", {
            "tp_pct": tp_pct, "sl_pct": sl_pct,
            "tp_value": tp_value, "sl_value": sl_value
        })

        # 5) Decision & open spread
        payload = {
            "type": side, "expiry": exp,
            "long": {"occ": long_leg["occ"], "strike": long_leg["strike"]},
            "short":{"occ": short_leg["occ"], "strike": short_leg["strike"]},
            "debit": debit_cost, "tp_value": tp_value, "sl_value": sl_value
        }
        _log_decision(logger, "ENTRY", "MACD crossover debit spread", payload)

        legs_data = [
            {"symbol": long_leg["occ"],  "side": "BUY",  "strike": long_leg["strike"],  "expiration": exp, "type": side},
            {"symbol": short_leg["occ"], "side": "SELL", "strike": short_leg["strike"], "expiration": exp, "type": side},
        ]

        try:
            trade = open_spread_trade(
                db=db,
                bot=bot,
                legs=legs_data,
                trade_type="BUY",       # buying the spread (paying debit)
                position_side=side,
                qty=bot.trade_size,
                entry_price=debit_cost, # net debit paid
                stop_loss=sl_value,     # spread value level
                take_profit=tp_value    # spread value level
            )
            if trade:
                bot.status = "TRADE OPEN: Debit Spread"
                try:
                    db.commit()
                except Exception:
                    db.rollback()
        except Exception:
            logger.exception("[DEBIT SPREAD] Bot #%s: Error opening spread", bot.id)

    logger.info("[ALGO:debit_spread_v1] end")
