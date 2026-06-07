# /var/www/stockwicks/app/scripts/options/algos/algo_credit_spread.py
import logging
import numpy as np
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
import pandas as pd
import pandas_ta as ta
from app.models.paper_option_trading_bot import PaperOptionTradeBot, PaperOptionBotOpenTrade
from app.utils.options.option_trade_utils import open_spread_trade
from app.utils.options.expiry_guard import filter_expiries


def _jdump(obj: Any, maxlen: int = 2000) -> str:
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

def calculate_iv_rank(chain, underlying_price, logger):
    """Calculate IV Rank (0-100) for the underlying"""
    try:
        # Get ATM options (within 5% of current price)
        atm_options = []
        for opt in chain:
            strike = _coerce_float(opt.get('strike'), 0)
            if strike and abs(strike - underlying_price) / underlying_price < 0.05:
                iv = _coerce_float(opt.get('impliedVolatility'), 0)
                if iv and iv > 0:
                    atm_options.append(iv)
        
        if not atm_options:
            return 30  # Default conservative value
            
        current_iv = np.mean(atm_options)
        
        # Simplified IV Rank calculation (in production, use historical IV data)
        # Assuming typical IV range between 0.15 and 0.45 for most stocks
        iv_rank = min(100, max(0, (current_iv - 0.15) / (0.45 - 0.15) * 100))
        
        _log_calc(logger, "iv_rank", {
            "current_iv": current_iv,
            "iv_rank": iv_rank,
            "samples": len(atm_options)
        })
        
        return iv_rank
    except Exception as e:
        logger.warning(f"[CREDIT SPREAD] IV Rank calculation failed: {e}")
        return 30

def calculate_position_size(bot, entry_price, stop_loss, max_risk_per_trade=0.01):
    """Calculate dynamic position sizing based on risk"""
    try:
        account_size = 10000000  # $10M account
        risk_amount = account_size * max_risk_per_trade
        risk_per_contract = abs(entry_price - stop_loss)
        
        if risk_per_contract <= 0:
            return 1
            
        max_contracts = int(risk_amount / risk_per_contract)
        return min(bot.trade_size, max_contracts, 10)  # Cap at 10 contracts
    except Exception:
        return 1

def run_credit_spread_bots(
    db: Session,
    bots: List[PaperOptionTradeBot],
    df: pd.DataFrame,
    chain: list,
    underlying_price: float,
    **kwargs,
):
    """
    credit_spread_v1 (PUT credit spread) - IMPROVED:
      - IV Rank filter (only trade when IV Rank > 30)
      - Multi-timeframe RSI confirmation
      - Volume confirmation
      - Dynamic position sizing
      - Better strike selection
    """
    logger: logging.Logger = kwargs.get("logger", logging.getLogger("option_algo_credit"))

    # ---------- Guards ----------
    u = _coerce_float(underlying_price, default=None)
    if u is None:
        logger.warning("[CREDIT SPREAD] No numeric underlying price; cannot run.")
        return
    if df is None or df.empty:
        logger.warning("[CREDIT SPREAD] Missing/empty minute bars; cannot run.")
        return
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            logger.error("[CREDIT SPREAD] '%s' column missing; cannot compute indicators.", col)
            return
    if not chain:
        logger.info("[CREDIT SPREAD] Empty chain; nothing to evaluate.")
        return

    # ---------- IV Rank Filter ----------
    iv_rank = calculate_iv_rank(chain, u, logger)
    if iv_rank < 30:
        _log_decision(logger, "NO-TRADE", "IV Rank too low for credit spreads", {"iv_rank": iv_rank})
        return

    # ---------- Multi-timeframe RSI ----------
    try:
        if not isinstance(df.index, pd.DatetimeIndex):
            if "datetime" in df.columns:
                df = df.copy()
                df.index = pd.to_datetime(df["datetime"])
                df = df.drop(columns=["datetime"])
            else:
                logger.error("[CREDIT SPREAD] DataFrame index is not DatetimeIndex and no 'datetime' column to coerce.")
                return

        # 15-min RSI
        df_15 = df.resample("15min").agg({
            "open": "first", "high": "max", "low": "min", 
            "close": "last", "volume": "sum"
        }).dropna()
        
        if len(df_15) < 15:
            logger.info("[CREDIT SPREAD] Not enough 15-min bars for RSI calculation.")
            return

        rsi_15 = ta.rsi(df_15["close"], length=14)
        rsi_15_val = _coerce_float(rsi_15.iloc[-1], default=None)
        
        # 1-hour RSI for confirmation
        df_1h = df.resample("1h").agg({
            "open": "first", "high": "max", "low": "min", 
            "close": "last", "volume": "sum"
        }).dropna()
        
        if len(df_1h) >= 14:
            rsi_1h = ta.rsi(df_1h["close"], length=14)
            rsi_1h_val = _coerce_float(rsi_1h.iloc[-1], default=None)
        else:
            rsi_1h_val = rsi_15_val

        if rsi_15_val is None or rsi_1h_val is None:
            logger.warning("[CREDIT SPREAD] RSI produced NaN; abort.")
            return
            
        _log_calc(logger, "multi_timeframe_rsi", {
            "rsi_15min": rsi_15_val,
            "rsi_1h": rsi_1h_val
        })
    except Exception:
        logger.exception("[CREDIT SPREAD] RSI calculation failed")
        return

    # ---------- Volume Confirmation ----------
    try:
        volume_sma = df['volume'].rolling(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / volume_sma.iloc[-1] if volume_sma.iloc[-1] > 0 else 1
    except Exception:
        volume_ratio = 1

    # Enhanced entry conditions
    rsi_neutral = (35 < rsi_15_val < 65) and (40 < rsi_1h_val < 60)
    volume_ok = volume_ratio > 0.8  # Not extremely low volume
    
    if not (rsi_neutral and volume_ok):
        _log_decision(logger, "NO-TRADE", "RSI or volume conditions not met", {
            "rsi_15": rsi_15_val, "rsi_1h": rsi_1h_val, "volume_ratio": volume_ratio
        })
        return

    # ---------- Sanitize chain & filter to PUTs ----------
    sanitized: list[Dict[str, Any]] = []
    for c in chain:
        try:
            put_call = (c.get("putCall") or "").upper()
            strike   = _coerce_float(c.get("strike"), None)
            bid      = _coerce_float(c.get("bid"), None)
            ask      = _coerce_float(c.get("ask"), None)
            delta    = _coerce_float(c.get("delta"), None)
            exp      = c.get("expiration")
            occ      = c.get("occ") or c.get("symbol")
            if (put_call == "PUT" and strike is not None and bid is not None and 
                ask is not None and exp and occ and strike < u * 0.95):  # Min 5% OTM
                sanitized.append({
                    "putCall": "PUT", "strike": strike,
                    "bid": bid, "ask": ask,
                    "delta": delta, "expiration": exp, "occ": occ
                })
        except Exception:
            continue

    # Expiry filtering
    all_exp_dates = list({_to_date(c.get("expiration")) for c in sanitized if c.get("expiration")})
    safe_exp_dates = filter_expiries([d for d in all_exp_dates if d], min_hours_to_expiry=24)
    safe_exp_strs = {d.strftime("%Y-%m-%d") for d in safe_exp_dates}
    puts = [c for c in sanitized if c["expiration"] in safe_exp_strs]

    _log_calc(logger, "chain_summary", {
        "total_chain": len(chain),
        "valid_puts": len(sanitized),
        "safe_expiries": sorted(list(safe_exp_strs))[:6],
        "puts_after_expiry_filter": len(puts),
    })

    if not puts:
        _log_decision(logger, "NO-TRADE", "no puts after expiry filtering", {})
        return

    # ---------- Iterate bots ----------
    for bot in bots:
        has_open = db.query(PaperOptionBotOpenTrade).filter_by(bot_id=bot.id).first() is not None
        if has_open:
            logger.info("[CREDIT SPREAD] Bot #%s: Open trade exists; skipping.", bot.id)
            continue

        params = bot.algo_params or {}
        width = _coerce_float(params.get("width"), 5.0) or 5.0
        if width <= 0:
            width = 5.0

        # Improved short put selection (25-35 delta, min 5% OTM)
        sp_cands = [p for p in puts if p.get("delta") is not None and 0.25 < abs(_coerce_float(p["delta"], 0)) < 0.35]
        if not sp_cands:
            # Fallback: select from top 10 OTM puts
            sp_cands = sorted([p for p in puts if p["strike"] < u], key=lambda c: c["strike"], reverse=True)[:10]
        if not sp_cands:
            _log_decision(logger, "NO-TRADE", "no short-put candidates", {})
            continue

        short_put = max(sp_cands, key=lambda c: c["strike"])  # Highest strike (closest to ATM)
        exp = short_put["expiration"]

        # Long put selection
        target_long_strike = short_put["strike"] - width
        same_exp_puts = [p for p in puts if p["expiration"] == exp]
        if not same_exp_puts:
            _log_decision(logger, "NO-TRADE", "no same-expiries for long leg", {"expiry": exp})
            continue
            
        try:
            long_put = min(same_exp_puts, key=lambda c: abs(c["strike"] - target_long_strike))
        except ValueError:
            long_put = None
            
        if not long_put:
            _log_decision(logger, "NO-TRADE", "no long put near target width", {"target_long_strike": target_long_strike})
            continue

        if abs(short_put["strike"] - long_put["strike"]) < 1e-6:
            _log_decision(logger, "NO-TRADE", "long/short identical strikes", {"strike": short_put["strike"]})
            continue

        # Price the spread
        short_price = _coerce_float(short_put.get("bid"), 0.0) or 0.0
        long_price  = _coerce_float(long_put.get("ask"), 0.0) or 0.0
        credit      = round(short_price - long_price, 2)
        width_eff   = abs(short_put["strike"] - long_put["strike"])
        max_loss    = round(width_eff - credit, 2) if width_eff > credit else width_eff

        # Enhanced quality checks
        min_credit = max(0.25, width_eff * 0.10)  # At least 10% of width
        if credit < min_credit or (max_loss > 0 and credit / max_loss < 0.30):
            _log_decision(
                logger, "NO-TRADE", "credit too low or poor R/R",
                {"credit": credit, "max_loss": max_loss, "width_eff": width_eff, "min_credit": min_credit}
            )
            continue

        _log_calc(logger, "spread_selection", {
            "expiry": exp,
            "short": {"occ": short_put["occ"], "strike": short_put["strike"], "bid": short_price, "delta": short_put.get("delta")},
            "long":  {"occ": long_put["occ"],  "strike": long_put["strike"],  "ask": long_price, "delta": long_put.get("delta")},
            "credit": credit, "width_eff": width_eff, "max_loss": max_loss
        })

        # Dynamic TP/SL based on volatility
        tp_pct = _coerce_float(params.get("tp_pct"), 50.0) or 50.0
        sl_pct = _coerce_float(params.get("sl_pct"), 100.0) or 100.0
        
        # Volatility-adjusted TP/SL
        try:
            atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
            atr_pct = atr / u
            # More aggressive TP in high vol, conservative in low vol
            if atr_pct > 0.02:  # High volatility
                tp_pct = min(tp_pct * 1.2, 70)
                sl_pct = min(sl_pct * 0.8, 80)
        except Exception:
            pass

        tp_value = max(0.01, round(credit * (1 - tp_pct / 100.0), 2))
        sl_value = round(credit * (1 + sl_pct / 100.0), 2)

        _log_calc(logger, "tp_sl_values", {
            "tp_pct": tp_pct, "sl_pct": sl_pct,
            "tp_value": tp_value, "sl_value": sl_value
        })

        # Dynamic position sizing
        dynamic_qty = calculate_position_size(bot, credit, sl_value, max_risk_per_trade=0.01)
        
        payload = {
            "type": "put", "expiry": exp,
            "short": {"occ": short_put["occ"], "strike": short_put["strike"]},
            "long":  {"occ": long_put["occ"],  "strike": long_put["strike"]},
            "credit": credit, "tp_value": tp_value, "sl_value": sl_value,
            "quantity": dynamic_qty
        }
        _log_decision(logger, "ENTRY", "neutral RSI put credit spread with IV filter", payload)

        legs_data = [
            {"symbol": short_put["occ"], "side": "SELL", "strike": short_put["strike"], "expiration": exp, "type": "put"},
            {"symbol": long_put["occ"],  "side": "BUY",  "strike": long_put["strike"],  "expiration": exp, "type": "put"},
        ]

        try:
            trade = open_spread_trade(
                db=db,
                bot=bot,
                legs=legs_data,
                trade_type="SELL",
                position_side="put",
                qty=dynamic_qty,  # Use dynamic sizing
                entry_price=credit,
                stop_loss=sl_value,
                take_profit=tp_value
            )
            if trade:
                bot.status = f"TRADE OPEN: Credit Spread x{dynamic_qty}"
                try:
                    db.commit()
                except Exception:
                    db.rollback()
        except Exception:
            logger.exception("[CREDIT SPREAD] Bot #%s: Error opening spread", bot.id)

    logger.info("[ALGO:credit_spread_v1] end")