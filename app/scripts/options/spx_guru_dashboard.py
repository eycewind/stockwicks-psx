#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/options/spx_guru_dashboard.py
"""
SPX 0DTE Guru Dashboard (CLI)
- Pulls SPX option chain from Schwab (/marketdata/v1/chains)
- Ranks 0DTE candidates for CREDIT (SELL) and/or DEBIT (BUY)
- Prints pretty console output OR emits JSON-only with --json

Key guarantees for web integration:
- When --json is set:
    * stdout contains JSON ONLY
    * logs go to stderr ONLY
"""

from __future__ import annotations

import os
import sys
import json
import math
import logging
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
from typing import Any, Dict, Optional, Tuple

import pytz
import requests
import pandas as pd

# --- Project Setup (ensure /var/www/stockwicks is on sys.path) ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# If you have a canonical token helper, keep it. Otherwise fallback will be used.
try:
    from app.utils.stock.schwab_token import get_valid_access_token  # type: ignore
except Exception:
    get_valid_access_token = None  # type: ignore

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
EASTERN = pytz.timezone("US/Eastern")

# IMPORTANT: log to stderr so JSON stays clean on stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("SPX_GURU")


# =========================
# Utils
# =========================

def _today_et() -> date:
    return datetime.now(EASTERN).date()

def _parse_date_input(s: Optional[str]) -> date:
    if not s or str(s).strip() == "":
        return _today_et()
    t = str(s).strip().lower()
    if t in ("today", "0dte", "0dte-today"):
        return _today_et()
    if t in ("tomorrow", "1dte"):
        return _today_et() + timedelta(days=1)
    # ISO
    return datetime.strptime(t, "%Y-%m-%d").date()

def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)

def _get_access_token_fallback(token_path: str = "/var/www/stockwicks/data/schwab_token.json") -> str:
    # You can swap to your preferred token file if different
    with open(token_path, "r") as f:
        return (json.load(f) or {}).get("access_token") or ""

def _get_access_token() -> str:
    if get_valid_access_token:
        try:
            return str(get_valid_access_token())
        except Exception:
            logger.exception("get_valid_access_token failed; falling back to token file")
    return _get_access_token_fallback()

def _headers() -> Dict[str, str]:
    tok = _get_access_token()
    if not tok:
        raise RuntimeError("No Schwab access token available.")
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"}


# =========================
# Schwab Fetchers
# =========================

def fetch_spx_option_chain(exp: date) -> Tuple[pd.DataFrame, float, Dict[str, Any]]:
    """
    Returns:
      df with columns: symbol, putCall, strike, expiration, bid, ask, mark, delta, iv, oi, volume, daysToExpiration
      underlying_price float
      raw chain json
    """
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": "$SPX",
        "contractType": "ALL",
        "strategy": "SINGLE",
        "range": "ALL",
        "includeUnderlyingQuote": "TRUE",
        "fromDate": exp.strftime("%Y-%m-%d"),
        "toDate": exp.strftime("%Y-%m-%d"),
    }

    r = requests.get(url, headers=_headers(), params=params, timeout=30)
    logger.info("Option chain API [$SPX %s] status=%s", exp.strftime("%Y-%m-%d"), r.status_code)
    if r.status_code != 200:
        raise RuntimeError(f"Option chain API failed: {r.status_code} {r.text[:500]}")
    data = r.json()

    # Underlying price: try common fields
    underlying = None
    uq = data.get("underlying") or data.get("underlyingQuote") or {}
    for k in ("last", "mark", "close", "bid", "ask"):
        if isinstance(uq, dict) and uq.get(k) is not None:
            underlying = float(uq.get(k))
            break
    if underlying is None:
        # fallback from top-level fields seen in some chain responses
        for k in ("underlyingPrice", "underlying", "underlyingLastPrice"):
            if data.get(k) is not None:
                try:
                    underlying = float(data.get(k))
                    break
                except Exception:
                    pass
    if underlying is None:
        underlying = float("nan")

    def _flatten(exp_map: Dict[str, Any], put_call: str) -> list:
        out = []
        for exp_key, strike_map in (exp_map or {}).items():
            # exp_key like "2026-01-12:0"
            exp_date = exp_key.split(":")[0]
            for strike_str, contracts in (strike_map or {}).items():
                if not contracts:
                    continue
                c = contracts[0]  # SINGLE strategy returns list of 1 per strike/side
                out.append({
                    "symbol": c.get("symbol"),
                    "putCall": put_call,
                    "strike": float(c.get("strikePrice") or c.get("strike") or strike_str),
                    "expiration": exp_date,
                    "bid": float(c.get("bid") or 0.0),
                    "ask": float(c.get("ask") or 0.0),
                    "mark": float(c.get("mark") or ((float(c.get("bid") or 0.0)+float(c.get("ask") or 0.0))/2 if (c.get("bid") is not None and c.get("ask") is not None) else 0.0)),
                    "delta": float(c.get("delta") or 0.0),
                    "iv": float(c.get("volatility") or c.get("impliedVolatility") or 0.0),
                    "oi": int(c.get("openInterest") or 0),
                    "volume": int(c.get("totalVolume") or c.get("volume") or 0),
                    "daysToExpiration": int(c.get("daysToExpiration") or 0),
                    "spread": float((float(c.get("ask") or 0.0) - float(c.get("bid") or 0.0))),
                })
        return out

    calls = _flatten(data.get("callExpDateMap") or {}, "CALL")
    puts = _flatten(data.get("putExpDateMap") or {}, "PUT")
    df = pd.DataFrame(calls + puts)
    if df.empty:
        raise RuntimeError("No contracts returned for SPX chain.")
    return df, underlying, data


def fetch_spx_pricehistory_15m(days: int = 3) -> Optional[pd.DataFrame]:
    """
    Fetch recent 15m candles; used for light momentum/vol metrics.
    If it fails, returns None.
    """
    try:
        url = f"{SCHWAB_API_URL}/pricehistory"
        end_ms = int(datetime.now(tz=pytz.UTC).timestamp() * 1000)
        start = datetime.now(tz=pytz.UTC) - timedelta(days=days)
        start_ms = int(start.timestamp() * 1000)
        params = {
            "symbol": "$SPX",
            "periodType": "day",
            "period": "3",
            "frequencyType": "minute",
            "frequency": "15",
            "startDate": str(start_ms),
            "endDate": str(end_ms),
            "needExtendedHoursData": "false",
        }
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
        logger.info("PriceHistory API [$SPX 15m] status=%s", r.status_code)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        candles = data.get("candles") or []
        if not candles:
            return None
        df = pd.DataFrame(candles)
        # Schwab uses "datetime" ms
        if "datetime" in df.columns:
            df["dt"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert(EASTERN)
        else:
            return None
        return df.sort_values("dt").reset_index(drop=True)
    except Exception:
        logger.exception("pricehistory failed")
        return None


# =========================
# Scoring / Picks
# =========================

def infer_strike_step(strikes: pd.Series) -> float:
    s = strikes.dropna().astype(float).sort_values().unique()
    if len(s) < 5:
        return 5.0
    diffs = pd.Series(s[1:] - s[:-1])
    step = float(diffs[diffs > 0].mode().iloc[0]) if (diffs > 0).any() else 5.0
    # clamp to reasonable
    if step <= 0:
        step = 5.0
    return step

def compute_momentum(underlying: float) -> Dict[str, Any]:
    df = fetch_spx_pricehistory_15m()
    if df is None or df.empty:
        return {"trend": "UNKNOWN", "score": 50, "volatility": "unknown"}
    closes = df["close"].astype(float).values
    if len(closes) < 30:
        return {"trend": "UNKNOWN", "score": 50, "volatility": "unknown"}

    # EMA fast/slow
    s = pd.Series(closes)
    ema_fast = s.ewm(span=8).mean().iloc[-1]
    ema_slow = s.ewm(span=21).mean().iloc[-1]
    # RSI-ish
    delta = s.diff()
    up = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    down = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rs = (up / down) if down and down > 0 else 999
    rsi = 100 - (100 / (1 + rs))

    # ATR-ish (use high/low/close if present)
    vol_label = "normal"
    if {"high", "low", "close"}.issubset(df.columns):
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        if underlying and underlying > 0 and atr:
            atr_pct = float(atr) / float(underlying)
            if atr_pct > 0.006:
                vol_label = "high"
            elif atr_pct < 0.003:
                vol_label = "low"

    trend = "NEUTRAL"
    if ema_fast > ema_slow and rsi >= 55:
        trend = "UP"
    elif ema_fast < ema_slow and rsi <= 45:
        trend = "DOWN"

    # Score: start at 50 then add points
    score = 50
    score += 15 if trend != "NEUTRAL" else 0
    score += 10 if (rsi >= 60 or rsi <= 40) else 0
    score += 5 if vol_label == "high" else 0
    score = max(1, min(99, int(score)))

    return {"trend": trend if trend != "UP" else "BULLISH" if trend=="UP" else trend, "score": score, "volatility": vol_label}

def compute_oi_pin(df: pd.DataFrame, underlying: float) -> Dict[str, Any]:
    # Window around underlying
    if not math.isfinite(underlying):
        return {"pin": float("nan"), "bias": "NEUTRAL", "pcr": None, "pin_confidence": "LOW"}

    w = df.copy()
    w["dist"] = (w["strike"].astype(float) - float(underlying)).abs()
    w = w.sort_values("dist").head(200)

    # group by strike
    g = w.groupby("strike", as_index=False).agg(
        oi_total=("oi", "sum"),
        oi_put=("oi", lambda x: int(w.loc[x.index][w.loc[x.index, "putCall"] == "PUT"]["oi"].sum()) if True else 0),
        oi_call=("oi", lambda x: int(w.loc[x.index][w.loc[x.index, "putCall"] == "CALL"]["oi"].sum()) if True else 0),
    )
    if g.empty:
        return {"pin": float("nan"), "bias": "NEUTRAL", "pcr": None, "pin_confidence": "LOW"}

    pin_row = g.sort_values("oi_total", ascending=False).iloc[0]
    pin = float(pin_row["strike"])
    oi_put = int(pin_row["oi_put"])
    oi_call = int(pin_row["oi_call"])
    pcr = (oi_put / oi_call) if oi_call > 0 else None

    bias = "NEUTRAL"
    if pcr is not None:
        if pcr > 1.2:
            bias = "BULLISH"
        elif pcr < 0.8:
            bias = "BEARISH"

    # confidence
    top = float(pin_row["oi_total"])
    total = float(g["oi_total"].sum()) if float(g["oi_total"].sum()) > 0 else 0.0
    conf = "LOW"
    if total > 0 and (top / total) >= 0.25 and top >= 5000:
        conf = "HIGH"
    elif total > 0 and (top / total) >= 0.15 and top >= 2000:
        conf = "MEDIUM"

    return {"pin": pin, "bias": bias, "pcr": pcr, "pin_confidence": conf}

def _liquidity_score(row: pd.Series) -> float:
    # tighter spreads, more volume/OI
    bid = float(row.get("bid", 0.0))
    ask = float(row.get("ask", 0.0))
    mid = (bid + ask) / 2 if (bid and ask) else float(row.get("mark", 0.0))
    spr = float(row.get("spread", 0.0))
    vol = float(row.get("volume", 0))
    oi = float(row.get("oi", 0))
    if mid <= 0:
        return 0.0
    spr_pct = spr / mid if mid > 0 else 1.0
    score = 100.0
    score -= min(60.0, spr_pct * 200.0)  # penalize wide
    score += min(25.0, math.log1p(vol) * 4.0)
    score += min(25.0, math.log1p(oi) * 3.0)
    return max(0.0, min(140.0, score))

def _safe_mid(row: pd.Series) -> float:
    bid = float(row.get("bid", 0.0))
    ask = float(row.get("ask", 0.0))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return float(row.get("mark", 0.0))

def pick_credit(df: pd.DataFrame, momentum: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    """
    CREDIT: SELL premium. Prefer ~10-25 delta, tight spreads, decent volume/OI.
    Returns best call and put dataframes sorted by score desc.
    """
    d = df.copy()
    d["delta_abs"] = d["delta"].abs()
    d["mid"] = d.apply(_safe_mid, axis=1)
    d["liq"] = d.apply(_liquidity_score, axis=1)

    # min premium filter
    d = d[(d["bid"] >= 0.25) & (d["bid"] <= 25.0)]
    # delta band
    d = d[(d["delta_abs"] >= 0.08) & (d["delta_abs"] <= 0.28)]
    # spread sanity
    d = d[(d["spread"] >= 0) & (d["spread"] <= 2.5)]

    # directional tilt for credit:
    # If bullish -> prefer selling puts; if bearish -> prefer selling calls; neutral -> both
    trend = str(momentum.get("trend", "NEUTRAL")).upper()
    tilt_put = 8.0 if trend in ("BULLISH", "UP") else 0.0
    tilt_call = 8.0 if trend in ("BEARISH", "DOWN") else 0.0

    def _score(row: pd.Series) -> float:
        base = row["liq"]
        base += (1.0 - abs(row["delta_abs"] - 0.18)) * 20.0
        if row["putCall"] == "PUT":
            base += tilt_put
        else:
            base += tilt_call
        # small preference for higher bid (more juice) but capped
        base += min(10.0, float(row["bid"]) * 0.8)
        return float(base)

    d["score"] = d.apply(_score, axis=1)
    calls = d[d["putCall"] == "CALL"].sort_values("score", ascending=False)
    puts = d[d["putCall"] == "PUT"].sort_values("score", ascending=False)
    return {"call": calls, "put": puts}

def pick_debit(df: pd.DataFrame, momentum: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    """
    DEBIT: BUY direction. Prefer 25-65 delta, tight spreads, decent volume.
    """
    d = df.copy()
    d["delta_abs"] = d["delta"].abs()
    d["mid"] = d.apply(_safe_mid, axis=1)
    d["liq"] = d.apply(_liquidity_score, axis=1)
    # price window
    d = d[(d["ask"] >= 1.0) & (d["ask"] <= 60.0)]
    d = d[(d["delta_abs"] >= 0.25) & (d["delta_abs"] <= 0.65)]
    d = d[(d["spread"] >= 0) & (d["spread"] <= 3.0)]

    trend = str(momentum.get("trend", "NEUTRAL")).upper()
    prefer_call = 10.0 if trend in ("BULLISH", "UP") else 0.0
    prefer_put = 10.0 if trend in ("BEARISH", "DOWN") else 0.0

    def _score(row: pd.Series) -> float:
        base = row["liq"]
        base += (1.0 - abs(row["delta_abs"] - 0.45)) * 22.0
        if row["putCall"] == "CALL":
            base += prefer_call
        else:
            base += prefer_put
        # small bonus for higher volume
        base += min(10.0, math.log1p(float(row.get("volume", 0))) * 2.0)
        return float(base)

    d["score"] = d.apply(_score, axis=1)
    calls = d[d["putCall"] == "CALL"].sort_values("score", ascending=False)
    puts = d[d["putCall"] == "PUT"].sort_values("score", ascending=False)
    return {"call": calls, "put": puts}


@dataclass
class TradePlan:
    style: str               # "credit" or "debit"
    action: str              # "SELL CALL", "SELL PUT", "BUY CALL", "BUY PUT"
    symbol: str
    strike: float
    expiration: str
    entry: float
    target1: float
    target2: float
    stop: float
    confidence: int
    confidence_level: str
    score: float
    contracts: int
    est_risk_usd: float
    est_reward1_usd: float
    est_reward2_usd: float
    notes: str

def _confidence_level(score: float) -> Tuple[int, str]:
    # normalize-ish
    s = max(1.0, min(99.0, score / 1.4))
    ci = int(round(s))
    if ci >= 80:
        return ci, "HIGH"
    if ci >= 65:
        return ci, "MEDIUM"
    return ci, "LOW"

def build_trade_plan(row: pd.Series, style: str, max_risk: float, contracts_override: Optional[int]) -> TradePlan:
    put_call = str(row["putCall"]).upper()
    strike = float(row["strike"])
    exp = str(row["expiration"])
    sym = str(row["symbol"])
    score = float(row.get("score", 0.0))

    if style == "credit":
        entry = float(row.get("bid", 0.0))  # sell at bid
        target1 = round(entry * 0.90, 2)
        target2 = round(entry * 0.80, 2)
        stop = round(entry * 1.60, 2)
        action = f"SELL {put_call}"
        # risk per contract = (stop-entry)*100
        risk_pc = max(0.01, (stop - entry) * 100.0)
        reward1_pc = max(0.0, (entry - target1) * 100.0)
        reward2_pc = max(0.0, (entry - target2) * 100.0)
        notes = "Credit (SELL) — manage by premium decay; exit by 3:55 PM ET."
    else:
        entry = float(row.get("ask", 0.0))  # buy at ask
        target1 = round(entry * 1.15, 2)
        target2 = round(entry * 1.30, 2)
        stop = round(entry * 0.60, 2)
        action = f"BUY {put_call}"
        risk_pc = max(0.01, (entry - stop) * 100.0)
        reward1_pc = max(0.0, (target1 - entry) * 100.0)
        reward2_pc = max(0.0, (target2 - entry) * 100.0)
        notes = "Debit (BUY) — directional; exit by 3:55 PM ET."

    if contracts_override and int(contracts_override) > 0:
        contracts = int(contracts_override)
    else:
        contracts = max(1, int(max_risk // risk_pc)) if risk_pc > 0 else 1
        contracts = max(1, min(50, contracts))

    conf, conf_level = _confidence_level(score)

    return TradePlan(
        style=style,
        action=action,
        symbol=sym,
        strike=strike,
        expiration=exp,
        entry=round(entry, 2),
        target1=round(target1, 2),
        target2=round(target2, 2),
        stop=round(stop, 2),
        confidence=conf,
        confidence_level=conf_level,
        score=round(score, 2),
        contracts=contracts,
        est_risk_usd=round(risk_pc * contracts, 2),
        est_reward1_usd=round(reward1_pc * contracts, 2),
        est_reward2_usd=round(reward2_pc * contracts, 2),
        notes=notes,
    )


def _print_header(title: str):
    print("\n" + "=" * 64)
    print(title.center(64))
    print("=" * 64)

def _print_plan(title: str, plan: TradePlan):
    _print_header(title)
    print(f"STYLE:      {plan.style.upper():<8} ({plan.action})")
    print(f"CONTRACT:   {plan.symbol:<18}  STRIKE {plan.strike:.2f}   EXP {plan.expiration}")
    print("-" * 64)
    print(f"Entry:      {plan.entry:.2f}")
    print(f"Target 1:   {plan.target1:.2f}")
    print(f"Target 2:   {plan.target2:.2f}")
    print(f"Stop:       {plan.stop:.2f}")
    print("-" * 64)
    print(f"Confidence: {plan.confidence_level} ({plan.confidence}/100) | Score: {plan.score}")
    print(f"Contracts:  {plan.contracts}")
    print(f"Est Risk:   ${plan.est_risk_usd:.2f}")
    print(f"Est R1:     ${plan.est_reward1_usd:.2f}")
    print(f"Est R2:     ${plan.est_reward2_usd:.2f}")
    print(f"Notes:      {plan.notes}")

def run_dashboard(date_str: Optional[str], mode: str, max_risk: float, contracts: Optional[int], json_only: bool) -> Dict[str, Any]:
    exp = _parse_date_input(date_str)
    df, underlying, _raw = fetch_spx_option_chain(exp)

    step = infer_strike_step(df["strike"])
    momentum = compute_momentum(underlying if math.isfinite(underlying) else float(0))
    pin = compute_oi_pin(df, underlying)

    picks: Dict[str, Optional[TradePlan]] = {
        "credit_put": None,
        "credit_call": None,
        "debit_call": None,
        "debit_put": None,
    }

    mode = (mode or "both").lower().strip()
    if mode not in ("credit", "debit", "both"):
        mode = "both"

    if mode in ("credit", "both"):
        cr = pick_credit(df, momentum)
        if not cr["call"].empty:
            picks["credit_call"] = build_trade_plan(cr["call"].iloc[0], "credit", max_risk, contracts)
        if not cr["put"].empty:
            picks["credit_put"] = build_trade_plan(cr["put"].iloc[0], "credit", max_risk, contracts)

    if mode in ("debit", "both"):
        db = pick_debit(df, momentum)
        if not db["call"].empty:
            picks["debit_call"] = build_trade_plan(db["call"].iloc[0], "debit", max_risk, contracts)
        if not db["put"].empty:
            picks["debit_put"] = build_trade_plan(db["put"].iloc[0], "debit", max_risk, contracts)

    out: Dict[str, Any] = {
        "symbol": "$SPX",
        "expiration": exp.strftime("%Y-%m-%d"),
        "underlying": round(float(underlying), 2) if math.isfinite(underlying) else None,
        "strike_step": float(step),
        "momentum": momentum,
        "pin": pin,
        "mode": mode,
        "picks": {k: (asdict(v) if v else None) for k, v in picks.items()},
        "timestamp": datetime.now(EASTERN).isoformat(),
    }

    if not json_only:
        _print_header("SPX 0DTE GURU DASHBOARD")
        print(f"Symbol:          {out['symbol']}")
        print(f"Expiration:      {out['expiration']}")
        print(f"Underlying:      {('$'+str(out['underlying'])) if out['underlying'] is not None else 'N/A'}")
        print(f"Strike step:     {out['strike_step']}")
        print(f"Momentum:        {momentum.get('trend')} | Score {momentum.get('score')}/100 | Vol {momentum.get('volatility')}")
        print(f"OI Pin:          {pin.get('pin')} | Bias {pin.get('bias')} | PCR {pin.get('pcr')} | PinConf {pin.get('pin_confidence')}")
        if picks["credit_put"] and mode in ("credit", "both"):
            _print_plan("BEST CREDIT PUT (SELL)", picks["credit_put"])
        if picks["credit_call"] and mode in ("credit", "both"):
            _print_plan("BEST CREDIT CALL (SELL)", picks["credit_call"])
        if picks["debit_call"] and mode in ("debit", "both"):
            _print_plan("BEST DEBIT CALL (BUY)", picks["debit_call"])
        if picks["debit_put"] and mode in ("debit", "both"):
            _print_plan("BEST DEBIT PUT (BUY)", picks["debit_put"])

    return out


def main():
    import argparse

    p = argparse.ArgumentParser(description="SPX 0DTE Guru Dashboard (Credit/Debit)")
    p.add_argument("--date", default=None, help="today | 0dte | tomorrow | YYYY-MM-DD (ET-based)")
    p.add_argument("--mode", choices=["credit", "debit", "both"], default="both", help="Which picks to generate")
    p.add_argument("--max-risk", type=float, default=100.0, help="Max risk in USD (used for sizing if --contracts blank)")
    p.add_argument("--contracts", type=int, default=None, help="Override contracts")
    p.add_argument("--json", action="store_true", help="Emit JSON only to stdout")

    args = p.parse_args()

    try:
        out = run_dashboard(
            date_str=args.date,
            mode=args.mode,
            max_risk=float(args.max_risk),
            contracts=args.contracts,
            json_only=bool(args.json),
        )
        if args.json:
            # JSON ONLY on stdout:
            sys.stdout.write(_json_dumps(out))
            sys.stdout.write("\n")
            sys.stdout.flush()
    except Exception as e:
        # For CLI: show error
        if args.json:
            # Web expects JSON; return structured error JSON
            err = {
                "error": str(e),
                "traceback": traceback.format_exc()[-4000:],
            }
            sys.stdout.write(_json_dumps(err))
            sys.stdout.write("\n")
            sys.stdout.flush()
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
