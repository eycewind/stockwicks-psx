# /var/www/stockwicks/app/scripts/options/algos/algo_scalper.py
import logging
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
import pandas as pd
import pandas_ta as ta
from app.models.paper_option_trading_bot import PaperOptionTradeBot, PaperOptionBotOpenTrade
from app.utils.options.option_trade_utils import open_option_trade


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


def calculate_position_size(bot, entry_price, stop_loss, max_risk_per_trade=0.01):
    """Calculate dynamic position sizing based on risk"""
    try:
        account_size = 10000000
        risk_amount = account_size * max_risk_per_trade
        risk_per_contract = abs(entry_price - stop_loss)
        
        if risk_per_contract <= 0:
            return 1
            
        max_contracts = int(risk_amount / risk_per_contract)
        return min(bot.trade_size, max_contracts, 8)
    except Exception:
        return 1


def has_rsi_divergence(df, rsi_series, lookback=10):
    """Check for RSI divergence"""
    try:
        if len(df) < lookback + 1:
            return None
            
        price_slice = df['close'].iloc[-lookback-1:]
        rsi_slice = rsi_series.iloc[-lookback-1:]
        
        # Bullish divergence: lower lows in price, higher lows in RSI
        price_lows = price_slice.rolling(3).min().dropna()
        rsi_lows = rsi_slice.rolling(3).min().dropna()
        
        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            price_trend = price_lows.iloc[-1] < price_lows.iloc[-2]
            rsi_trend = rsi_lows.iloc[-1] > rsi_lows.iloc[-2]
            
            if price_trend and rsi_trend:
                return "bullish"
        
        # Bearish divergence: higher highs in price, lower highs in RSI
        price_highs = price_slice.rolling(3).max().dropna()
        rsi_highs = rsi_slice.rolling(3).max().dropna()
        
        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            price_trend = price_highs.iloc[-1] > price_highs.iloc[-2]
            rsi_trend = rsi_highs.iloc[-1] < rsi_highs.iloc[-2]
            
            if price_trend and rsi_trend:
                return "bearish"
                
        return None
    except Exception:
        return None


def run_scalper_bots(
    db: Session,
    bots: List[PaperOptionTradeBot],
    df: pd.DataFrame,
    chain: list,
    underlying_price: float,
    **kwargs,
):
    """
    scalper_v1: IMPROVED - Buys options with RSI divergence + momentum confirmation
    """
    logger: logging.Logger = kwargs.get("logger", logging.getLogger("option_algo_scalper"))

    # ---------- Guards ----------
    u = _coerce_float(underlying_price, default=None)
    if u is None:
        logger.warning("[SCALPER] No numeric underlying price; cannot run.")
        return
    if df is None or df.empty:
        logger.warning("[SCALPER] Missing/empty minute bars; cannot run.")
        return
    if "close" not in df.columns or "volume" not in df.columns:
        logger.error("[SCALPER] Required columns missing; cannot compute indicators.")
        return
    if not chain:
        logger.info("[SCALPER] Empty chain; nothing to evaluate.")
        return

    # ---------- Enhanced RSI with Divergence ----------
    try:
        # Ensure datetime index
        if not isinstance(df.index, pd.DatetimeIndex):
            if "datetime" in df.columns:
                df = df.copy()
                df.index = pd.to_datetime(df["datetime"])
            else:
                logger.error("[SCALPER] No datetime index available.")
                return

        rsi_series = ta.rsi(df["close"], length=14)
        df = df.copy()
        df["RSI"] = rsi_series
        rsi_val = _coerce_float(df["RSI"].iloc[-1], default=None)
        
        if rsi_val is None:
            logger.warning("[SCALPER] RSI produced NaN; aborting this tick.")
            return
            
        # RSI divergence check
        divergence = has_rsi_divergence(df, rsi_series, lookback=14)
        
        # Volume confirmation
        volume_sma = df['volume'].rolling(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / volume_sma.iloc[-1] if volume_sma.iloc[-1] > 0 else 1
        
        # Momentum confirmation (recent price action)
        price_change_5 = (df['close'].iloc[-1] - df['close'].iloc[-5]) / df['close'].iloc[-5] if len(df) >= 5 else 0
        
        _log_calc(logger, "enhanced_rsi", {
            "rsi_14": rsi_val,
            "divergence": divergence,
            "volume_ratio": volume_ratio,
            "price_change_5m": price_change_5
        })
    except Exception:
        logger.exception("[SCALPER] RSI calculation failed")
        return

    # ---------- Enhanced Signal Logic ----------
    signal_type = None
    signal_strength = 0
    
    # Bullish conditions
    if rsi_val < 35 and divergence == "bullish" and volume_ratio > 1.0 and price_change_5 < 0:
        signal_type = "CALL"
        signal_strength = (35 - rsi_val) / 35
        
    # Bearish conditions  
    elif rsi_val > 65 and divergence == "bearish" and volume_ratio > 1.0 and price_change_5 > 0:
        signal_type = "PUT"
        signal_strength = (rsi_val - 65) / 35
        
    if not signal_type or signal_strength < 0.3:
        _log_decision(logger, "NO-TRADE", "Weak or unconfirmed signal", {
            "rsi": rsi_val, "divergence": divergence, 
            "volume_ratio": volume_ratio, "signal_strength": signal_strength
        })
        return

    # ---------- Sanitize/prepare chain ----------
    sanitized = []
    for c in chain:
        try:
            put_call = (c.get("putCall") or "").upper()
            strike = _coerce_float(c.get("strike"), None)
            ask = _coerce_float(c.get("ask"), None)
            bid = _coerce_float(c.get("bid"), None)
            mark = _coerce_float(c.get("mark"), ask if ask is not None else None)
            exp = c.get("expiration")
            occ = c.get("occ") or c.get("symbol")
            
            if (put_call in ("CALL", "PUT") and strike is not None and 
                ask is not None and exp and occ and ask > 0.10):
                sanitized.append({
                    "putCall": put_call, "strike": strike,
                    "ask": ask, "bid": bid, "mark": mark,
                    "expiration": exp, "occ": occ
                })
        except Exception:
            continue

    # Filter by signal side and reasonable strikes
    if signal_type == "CALL":
        candidates = [c for c in sanitized if c["putCall"] == "CALL" and c["strike"] > u * 0.98]
    else:
        candidates = [c for c in sanitized if c["putCall"] == "PUT" and c["strike"] < u * 1.02]

    _log_calc(logger, "chain_candidates", {
        "signal": signal_type,
        "signal_strength": signal_strength,
        "total_chain": len(chain),
        "valid_sanitized": len(sanitized),
        f"{signal_type}_count": len(candidates),
    })

    if not candidates:
        _log_decision(logger, "NO-TRADE", f"no {signal_type} contracts meeting criteria", {})
        return

    # ---------- ATM selection with quality filter ----------
    try:
        # Prefer slightly OTM for better risk/reward
        if signal_type == "CALL":
            atm_contract = min([c for c in candidates if c["strike"] >= u], 
                              key=lambda x: abs(x["strike"] - u * 1.01))
        else:
            atm_contract = min([c for c in candidates if c["strike"] <= u], 
                              key=lambda x: abs(x["strike"] - u * 0.99))
    except Exception:
        logger.exception("[SCALPER] Failed to select optimal contract")
        return

    # ---------- Iterate each bot ----------
    for bot in bots:
        has_open = db.query(PaperOptionBotOpenTrade).filter_by(bot_id=bot.id).first() is not None
        if has_open:
            logger.info("[SCALPER] Bot #%s: Open trade exists; skipping.", bot.id)
            continue

        # Entry price validation
        entry = _coerce_float(atm_contract.get("ask"), 0.0) or 0.0
        min_entry = u * 0.002
        if entry < min_entry:
            _log_decision(logger, "NO-TRADE", "ask/entry too low", {"ask": entry, "min_entry": min_entry})
            continue

        params = bot.algo_params or {}
        tp_pct = _coerce_float(params.get("tp_pct"), 25.0)
        sl_pct = _coerce_float(params.get("sl_pct"), 25.0)
        if not tp_pct or tp_pct <= 0:
            tp_pct = 25.0
        if not sl_pct or sl_pct <= 0:
            sl_pct = 25.0

        # Volatility-adjusted TP/SL
        try:
            atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
            atr_pct = atr / u
            if atr_pct > 0.02:
                tp_pct = min(tp_pct * 1.5, 50)
                sl_pct = min(sl_pct * 1.2, 40)
        except Exception:
            pass

        # Signal strength adjustment
        tp_pct = tp_pct * (1 + signal_strength)
        sl_pct = sl_pct * (1 - signal_strength * 0.5)

        tp_price = round(entry * (1 + tp_pct / 100.0), 2)
        sl_price = max(0.01, round(entry * (1 - sl_pct / 100.0), 2))

        # Dynamic position sizing
        dynamic_qty = calculate_position_size(bot, entry, sl_price, max_risk_per_trade=0.01)

        _log_calc(logger, "tp_sl_prices", {
            "signal": signal_type, "entry_ask": entry,
            "tp_pct": tp_pct, "sl_pct": sl_pct,
            "tp_price": tp_price, "sl_price": sl_price,
            "quantity": dynamic_qty,
            "signal_strength": signal_strength
        })

        # Decision & Open
        payload = {
            "side": signal_type,
            "strike": atm_contract["strike"],
            "occ": atm_contract["occ"],
            "expiry": atm_contract["expiration"],
            "entry": entry,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "quantity": dynamic_qty,
            "signal_strength": signal_strength
        }
        _log_decision(logger, "ENTRY", "RSI extreme with divergence confirmation", payload)

        try:
            trade = open_option_trade(
                db=db,
                bot=bot,
                option_symbol=atm_contract["occ"],
                underlying=bot.symbol,
                trade_type="BUY",
                position_side=signal_type.lower(),
                qty=dynamic_qty,
                entry_price=entry,
                strike=atm_contract["strike"],
                expiry=atm_contract["expiration"],
                take_profit=tp_price,
                stop_loss=sl_price,
            )
            if trade:
                bot.status = f"TRADE OPEN: Bought {signal_type} {atm_contract['strike']} x{dynamic_qty}"
                try:
                    db.commit()
                except Exception:
                    db.rollback()
        except Exception as e:
            logger.exception("[SCALPER] Bot #%s: Error executing trade: %s", bot.id, e)

    logger.info("[ALGO:scalper_v1] end")