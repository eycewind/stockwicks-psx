# /var/www/stockwicks/app/scripts/options/algos/algo_guru.py
import logging
import numpy as np
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
import pandas as pd
import pandas_ta as ta
from app.models.paper_option_trading_bot import PaperOptionTradeBot, PaperOptionBotOpenTrade
from app.utils.options.option_trade_utils import open_option_trade


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

def calculate_iv_rank(chain, underlying_price, logger):
    """Calculate IV Rank (0-100) for the underlying"""
    try:
        atm_options = []
        for opt in chain:
            strike = _coerce_float(opt.get('strike'), 0)
            if strike and abs(strike - underlying_price) / underlying_price < 0.05:
                iv = _coerce_float(opt.get('impliedVolatility'), 0)
                if iv and iv > 0:
                    atm_options.append(iv)
        
        if not atm_options:
            return 30
            
        current_iv = np.mean(atm_options)
        iv_rank = min(100, max(0, (current_iv - 0.15) / (0.45 - 0.15) * 100))
        
        _log_calc(logger, "iv_rank", {
            "current_iv": current_iv,
            "iv_rank": iv_rank,
            "samples": len(atm_options)
        })
        
        return iv_rank
    except Exception as e:
        logger.warning(f"[GURU] IV Rank calculation failed: {e}")
        return 30

def calculate_position_size(bot, entry_price, stop_loss, max_risk_per_trade=0.01):
    """Calculate dynamic position sizing based on risk"""
    try:
        account_size = 10000000
        risk_amount = account_size * max_risk_per_trade
        risk_per_contract = abs(entry_price - stop_loss)
        
        if risk_per_contract <= 0:
            return 1
            
        max_contracts = int(risk_amount / risk_per_contract)
        return min(bot.trade_size, max_contracts, 5)  # More conservative for naked options
    except Exception:
        return 1

def run_guru_bots(
    db: Session,
    bots: List[PaperOptionTradeBot],
    df: pd.DataFrame,
    chain: list,
    underlying_price: float,
    **kwargs,
):
    """
    guru_v1: IMPROVED - Sells premium with multiple confirmations
      - IV Rank filter (only sell when IV Rank > 40)
      - Multi-timeframe VWAP confirmation
      - Volume confirmation
      - Price action filters
      - Dynamic position sizing
    """
    logger: logging.Logger = kwargs.get("logger", logging.getLogger("option_algo_guru"))

    # ---------- Guards ----------
    if underlying_price is None:
        logger.warning("[GURU] Missing underlying price; cannot run.")
        return
    if df is None or df.empty:
        logger.warning("[GURU] Missing/empty minute bars; cannot run.")
        return
    if not chain:
        logger.warning("[GURU] Empty option chain; cannot run.")
        return

    u = _coerce_float(underlying_price, default=None)
    if u is None:
        logger.warning("[GURU] Underlying price not numeric; abort.")
        return

    # ---------- IV Rank Filter ----------
    iv_rank = calculate_iv_rank(chain, u, logger)
    if iv_rank < 40:  # Higher threshold for selling premium
        _log_decision(logger, "NO-TRADE", "IV Rank too low for premium selling", {"iv_rank": iv_rank})
        return

    # ---------- Multi-timeframe VWAP + Volume ----------
    for col in ("high", "low", "close", "volume"):
        if col not in df.columns:
            logger.error("[GURU] '%s' column missing; cannot calculate indicators.", col)
            return

    try:
        # Ensure datetime index
        if not isinstance(df.index, pd.DatetimeIndex):
            if "datetime" in df.columns:
                df = df.copy()
                df.index = pd.to_datetime(df["datetime"])
            else:
                logger.error("[GURU] No datetime index available.")
                return

        # 15-min VWAP
        vwap_15m = ta.vwap(high=df["high"], low=df["low"], close=df["close"], volume=df["volume"])
        
        # 1-hour VWAP for confirmation
        df_1h = df.resample("1h").agg({
            "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        vwap_1h = ta.vwap(high=df_1h["high"], low=df_1h["low"], close=df_1h["close"], volume=df_1h["volume"])
        
        vwap_15m_last = _coerce_float(vwap_15m.iloc[-1], default=None)
        vwap_1h_last = _coerce_float(vwap_1h.iloc[-1], default=None) if not vwap_1h.empty else vwap_15m_last
        last_price = _coerce_float(df["close"].iloc[-1], default=None)
        
        # Multi-timeframe trend confirmation
        is_uptrend = (last_price is not None and vwap_15m_last is not None and 
                     vwap_1h_last is not None and last_price > vwap_15m_last and last_price > vwap_1h_last)

        # Volume confirmation
        volume_sma = df['volume'].rolling(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / volume_sma.iloc[-1] if volume_sma.iloc[-1] > 0 else 1
        
        # Price action filter - avoid selling into strong trends
        price_ma_20 = df['close'].rolling(20).mean().iloc[-1]
        price_ma_50 = df['close'].rolling(50).mean().iloc[-1]
        trend_strength = abs((price_ma_20 - price_ma_50) / price_ma_50)
        
        _log_calc(logger, "underlying_indicators", {
            "close_last": last_price,
            "vwap_15m": vwap_15m_last,
            "vwap_1h": vwap_1h_last,
            "uptrend": bool(is_uptrend),
            "volume_ratio": volume_ratio,
            "trend_strength": trend_strength,
            "iv_rank": iv_rank
        })
    except Exception as e:
        logger.exception("[GURU] Indicator calculation failed: %s", e)
        return

    # Enhanced entry conditions
    volume_ok = volume_ratio > 0.8
    low_trend_strength = trend_strength < 0.03  # Avoid strong trending markets
    
    if not (volume_ok and low_trend_strength):
        _log_decision(logger, "NO-TRADE", "Volume or trend conditions not met", {
            "volume_ratio": volume_ratio, "trend_strength": trend_strength
        })
        return

    # ---------- Candidate selection ----------
    sanitized: list[Dict[str, Any]] = []
    for c in chain:
        try:
            put_call = (c.get("putCall") or "").upper()
            strike = _coerce_float(c.get("strike"), None)
            delta = _coerce_float(c.get("delta"), None)
            bid = _coerce_float(c.get("bid"), None)
            ask = _coerce_float(c.get("ask"), None)
            mark = _coerce_float(c.get("mark"), bid if bid is not None else None)
            exp = c.get("expiration")
            occ = c.get("occ") or c.get("symbol")
            
            if (put_call in ("CALL", "PUT") and strike is not None and delta is not None and 
                bid is not None and mark is not None and exp):
                # Additional filters for quality
                if put_call == "PUT" and strike < u * 0.90:  # Min 10% OTM for puts
                    sanitized.append({
                        "putCall": put_call, "strike": strike, "delta": delta,
                        "bid": bid, "ask": ask, "mark": mark, "expiration": exp, "occ": occ
                    })
                elif put_call == "CALL" and strike > u * 1.10:  # Min 10% OTM for calls
                    sanitized.append({
                        "putCall": put_call, "strike": strike, "delta": delta,
                        "bid": bid, "ask": ask, "mark": mark, "expiration": exp, "occ": occ
                    })
        except Exception:
            continue

    _log_calc(logger, "chain_summary", {
        "received": len(chain),
        "valid": len(sanitized),
    })

    # Target delta range for selling (25-35)
    tgt_delta_min = 0.25
    tgt_delta_max = 0.35

    def choose_otm_put():
        cands = [c for c in sanitized if c["putCall"] == "PUT" and c["strike"] < u]
        # Prefer strikes within target delta range
        in_range = [c for c in cands if tgt_delta_min < abs(c["delta"]) < tgt_delta_max]
        if in_range:
            return max(in_range, key=lambda c: c["bid"])  # Highest premium
        return min(cands, key=lambda c: abs(abs(c["delta"]) - 0.30)) if cands else None

    def choose_otm_call():
        cands = [c for c in sanitized if c["putCall"] == "CALL" and c["strike"] > u]
        in_range = [c for c in cands if tgt_delta_min < c["delta"] < tgt_delta_max]
        if in_range:
            return max(in_range, key=lambda c: c["bid"])
        return min(cands, key=lambda c: abs(c["delta"] - 0.30)) if cands else None

    # ---------- Iterate bots ----------
    for bot in bots:
        has_open = db.query(PaperOptionBotOpenTrade).filter_by(bot_id=bot.id).first() is not None
        if has_open:
            logger.info("[GURU] Bot #%s: Open trade exists; skipping.", bot.id)
            continue

        # Select candidate based on trend
        if is_uptrend:
            logger.info("[GURU] Bot #%s: Uptrend confirmed. Seeking OTM PUT.", bot.id)
            choice = choose_otm_put()
        else:
            logger.info("[GURU] Bot #%s: Downtrend. Seeking OTM CALL.", bot.id)
            choice = choose_otm_call()

        if not choice:
            _log_decision(logger, "NO-TRADE", "no suitable contract near target delta", {
                "tgt_delta_range": [tgt_delta_min, tgt_delta_max], "uptrend": bool(is_uptrend)
            })
            bot.status = "RUNNING - No Candidate"
            try:
                db.commit()
            except Exception:
                db.rollback()
            continue

        # Price validation
        credit = _coerce_float(choice.get("bid"), 0.0) or 0.0
        min_credit = u * 0.005  # At least 0.5% of underlying
        if credit < min_credit:
            _log_decision(logger, "NO-TRADE", "credit too low", {"credit": credit, "min_credit": min_credit})
            continue

        params = bot.algo_params or {}
        tp_pct = _coerce_float(params.get("tp_pct"), 50.0)
        sl_pct = _coerce_float(params.get("sl_pct"), 100.0)
        if not tp_pct or tp_pct <= 0:
            tp_pct = 50.0
        if not sl_pct or sl_pct <= 0:
            sl_pct = 100.0

        # Volatility-adjusted TP/SL
        try:
            atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
            atr_pct = atr / u
            if atr_pct > 0.025:  # High volatility - wider stops
                sl_pct = min(sl_pct * 1.2, 150)
        except Exception:
            pass

        tp_price = max(0.01, round(credit * (1 - tp_pct / 100.0), 2))
        sl_price = round(credit * (1 + sl_pct / 100.0), 2)

        # Dynamic position sizing
        dynamic_qty = calculate_position_size(bot, credit, sl_price, max_risk_per_trade=0.01)

        _log_calc(logger, "tp_sl_prices", {
            "credit_bid": credit, "tp_pct": tp_pct, "sl_pct": sl_pct,
            "tp_price": tp_price, "sl_price": sl_price,
            "quantity": dynamic_qty
        })

        # Final decision
        payload = {
            "side": choice["putCall"],
            "strike": choice["strike"],
            "delta": choice["delta"],
            "occ": choice["occ"],
            "expiry": choice["expiration"],
            "credit": credit,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "quantity": dynamic_qty
        }
        _log_decision(logger, "ENTRY", "sell premium with multiple confirmations", payload)

        try:
            trade = open_option_trade(
                db=db,
                bot=bot,
                option_symbol=choice["occ"],
                underlying=bot.symbol,
                trade_type="SELL",
                position_side=choice["putCall"].lower(),
                qty=dynamic_qty,  # Dynamic sizing
                entry_price=credit,
                strike=choice["strike"],
                expiry=choice["expiration"],
                take_profit=tp_price,
                stop_loss=sl_price,
            )
            if trade:
                bot.status = f"TRADE OPEN: Sold {choice['putCall']} {choice['strike']} x{dynamic_qty}"
                try:
                    db.commit()
                except Exception:
                    db.rollback()
        except Exception as e:
            logger.exception("[GURU] Bot #%s: Error executing trade: %s", bot.id, e)

    logger.info("[ALGO:guru_v1] end")