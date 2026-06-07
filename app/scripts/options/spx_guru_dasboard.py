#!/usr/bin/env python3
"""
spx_guru_dashboard.py — SPX 0DTE "Guru" (Schwab) | CREDIT + DEBIT picks

What this does:
- Pulls the SPX option chain for a chosen expiration (default: 0DTE / today ET)
- Computes:
    (1) OI-based "pin" + key walls (lightweight MM-style)
    (2) intraday momentum regime (15m RSI/EMA/ATR)
- Produces TWO sets of picks:
    - CREDIT (SELL to open): best call/put candidates for premium selling
    - DEBIT  (BUY  to open): best call/put candidates for directional day-trades

This script is designed to be "drop-in" alongside your existing Options Guru codebase.
It follows your existing conventions:
- Uses Schwab /marketdata/v1/chains (same endpoint as your SPX predictor)
- Uses single-leg contracts (strategy=SINGLE)
- Uses entry=bid for SELL and entry=ask for BUY
- Targets are % premium moves and exits are by EOD

Notes:
- If Schwab returns zero/near-zero OI (can happen), this script still works but the "pin" confidence is reduced.
- SPX strikes are typically in 5/10/25 increments depending on listing; we infer the dominant increment from the chain.

CLI examples:
  python spx_guru_dashboard.py
  python spx_guru_dashboard.py --date 2026-01-12
  python spx_guru_dashboard.py --date today --json
  python spx_guru_dashboard.py --contracts 2 --max-risk 200
"""

from __future__ import annotations

import os
import sys
import json
import math
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
from typing import Dict, Any, Optional, Tuple, List

import requests
import pandas as pd
import pytz

try:
    import pandas_ta as ta  # optional but recommended
except Exception:  # pragma: no cover
    ta = None

# --- Project root (so we can import your Schwab token helper if present) ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
EASTERN = pytz.timezone("US/Eastern")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SPX_GURU")


# =========================
# Token / Headers
# =========================
def _get_access_token_fallback(token_path: str = "/var/www/stockwicks/data/schwab_token.json") -> str:
    with open(token_path, "r") as f:
        return json.load(f).get("access_token") or ""


def get_schwab_headers() -> Dict[str, str]:
    """
    Prefer your existing helper if installed; fall back to reading the JSON token.
    """
    try:
        from app.utils.stock.schwab_token import get_valid_access_token  # type: ignore

        tok = get_valid_access_token()
        if not tok:
            raise ValueError("Empty token from get_valid_access_token()")
        return {"Authorization": f"Bearer {tok}"}
    except Exception:
        tok = _get_access_token_fallback()
        if not tok:
            raise ValueError("Could not get Schwab access token (helper + fallback failed).")
        return {"Authorization": f"Bearer {tok}"}


# =========================
# Date helpers
# =========================
def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _next_weekday(d: date) -> date:
    while _is_weekend(d):
        d = d + timedelta(days=1)
    return d


def parse_date_input(date_input: Optional[str]) -> date:
    """
    Accepts: None, today, 0dte, tomorrow, 1dte, YYYY-MM-DD
    Uses ET for "today/tomorrow".
    """
    now_et = datetime.now(EASTERN)
    today = now_et.date()

    if not date_input:
        return _next_weekday(today)

    s = str(date_input).strip().lower()

    if s in {"today", "0dte", "0", "now"}:
        return _next_weekday(today)
    if s in {"tomorrow", "1dte", "1", "+1"}:
        return _next_weekday(today + timedelta(days=1))

    # YYYY-MM-DD
    try:
        return _next_weekday(datetime.strptime(s, "%Y-%m-%d").date())
    except Exception:
        return _next_weekday(today)


# =========================
# Schwab: option chain (SPX)
# =========================
def fetch_spx_option_chain(expiration: date) -> Tuple[pd.DataFrame, float, Dict[str, Any]]:
    """
    Pull SPX chain for a specific expiration using Schwab:
    - endpoint: /marketdata/v1/chains
    - symbol: "$SPX" (this is what your predictor uses)
    """
    url = f"{SCHWAB_API_URL}/chains"
    params = {
        "symbol": "$SPX",
        "fromDate": expiration.strftime("%Y-%m-%d"),
        "toDate": expiration.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "range": "ALL",
        "contractType": "ALL",
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    logger.info(f"Option chain API [$SPX {params['fromDate']}] status={resp.status_code}")
    resp.raise_for_status()
    chain = resp.json()

    # Underlying price: Schwab responses are inconsistent across symbols
    price = (
        chain.get("underlyingPrice")
        or (chain.get("underlyingQuote") or {}).get("lastPrice")
        or (chain.get("underlyingQuote") or {}).get("mark")
        or (chain.get("underlying") or {}).get("last")
        or (chain.get("underlyingQuote") or {}).get("askPrice")
        or (chain.get("underlyingQuote") or {}).get("bidPrice")
    )
    if not price:
        raise ValueError("Could not infer SPX underlying price from chain response.")

    options: List[Dict[str, Any]] = []

    def _append(side: str, contract: Dict[str, Any], strike_hint: Optional[float] = None):
        # Contract keys vary a bit; normalize carefully.
        sym = (contract.get("symbol") or "").replace(" ", "")
        strike = contract.get("strikePrice") or contract.get("strike") or strike_hint
        if strike is None:
            return
        bid = contract.get("bid")
        ask = contract.get("ask")
        if bid is None or ask is None:
            return

        options.append(
            {
                "symbol": sym,
                "side": side,  # call / put
                "strike": float(strike),
                "bid": float(bid),
                "ask": float(ask),
                "mark": float(contract.get("mark", (float(bid) + float(ask)) / 2.0)),
                "iv": float(contract.get("volatility", contract.get("iv", 0.0)) or 0.0),
                "delta": float(contract.get("delta", 0.0) or 0.0),
                "gamma": float(contract.get("gamma", 0.0) or 0.0),
                "oi": int(contract.get("openInterest", 0) or 0),
                "volume": int(contract.get("totalVolume", 0) or 0),
                "inTheMoney": bool(contract.get("inTheMoney", False)),
                "daysToExpiration": int(contract.get("daysToExpiration", 0) or 0),
            }
        )

    for side, key in [("call", "callExpDateMap"), ("put", "putExpDateMap")]:
        for exp_key, strikes in (chain.get(key) or {}).items():
            # exp_key looks like "YYYY-MM-DD:0" sometimes
            exp_date = str(exp_key).split(":")[0]
            if exp_date != expiration.strftime("%Y-%m-%d"):
                continue
            for strike_str, contracts in (strikes or {}).items():
                try:
                    strike_hint = float(strike_str)
                except Exception:
                    strike_hint = None
                if not contracts:
                    continue
                for c in contracts:
                    _append(side, c, strike_hint=strike_hint)

    df = pd.DataFrame(options)
    if df.empty:
        raise ValueError("No SPX option contracts parsed from chain response.")

    return df, float(price), chain


def infer_strike_step(strikes: pd.Series) -> float:
    """
    Infer dominant strike increment from the chain.
    """
    s = sorted(set([float(x) for x in strikes.dropna().tolist()]))
    if len(s) < 5:
        return 25.0
    diffs = [round(s[i + 1] - s[i], 6) for i in range(len(s) - 1)]
    diffs = [d for d in diffs if d > 0]
    if not diffs:
        return 25.0
    # Most common diff
    return float(pd.Series(diffs).mode().iloc[0])


# =========================
# OI-based "pin" + walls
# =========================
def compute_oi_pin(df: pd.DataFrame, underlying: float) -> Dict[str, Any]:
    """
    Lightweight pin estimator:
    - Total OI per strike within ±5% range
    - Pick strike with max total OI as "pin"
    - Compute put/call ratio for bias
    """
    work = df.copy()
    work["abs_dist_pct"] = (work["strike"] - underlying).abs() / underlying
    work = work[work["abs_dist_pct"] <= 0.05].copy()

    if work.empty:
        return {
            "pin": underlying,
            "pin_confidence": "LOW",
            "put_call_ratio": None,
            "bias": "NEUTRAL",
            "walls": [],
        }

    # Put/call OI
    call_oi = int(work.loc[work["side"] == "call", "oi"].sum())
    put_oi = int(work.loc[work["side"] == "put", "oi"].sum())
    pcr = (put_oi / call_oi) if call_oi > 0 else None

    if pcr is None:
        bias = "NEUTRAL"
    elif pcr > 1.3:
        bias = "BEARISH"
    elif pcr < 0.7:
        bias = "BULLISH"
    else:
        bias = "NEUTRAL"

    strike_oi = work.groupby("strike", as_index=False)["oi"].sum().sort_values("oi", ascending=False)
    pin = float(strike_oi.iloc[0]["strike"])

    total_oi = int(strike_oi["oi"].sum())
    top_share = float(strike_oi.iloc[0]["oi"]) / total_oi if total_oi > 0 else 0.0

    if total_oi < 1000:
        pin_conf = "LOW"
    elif top_share > 0.06:
        pin_conf = "HIGH"
    elif top_share > 0.035:
        pin_conf = "MEDIUM"
    else:
        pin_conf = "LOW"

    # Walls: top 6 strikes by total OI, label call wall / put wall by dominance
    walls = []
    top = strike_oi.head(8)
    for _, row in top.iterrows():
        k = float(row["strike"])
        tot = int(row["oi"])
        call_at = int(work[(work["strike"] == k) & (work["side"] == "call")]["oi"].sum())
        put_at = int(work[(work["strike"] == k) & (work["side"] == "put")]["oi"].sum())
        if call_at > put_at * 1.5:
            typ = "CALL_WALL"
        elif put_at > call_at * 1.5:
            typ = "PUT_WALL"
        else:
            typ = "RESISTANCE" if k > underlying else "SUPPORT"
        walls.append({"strike": k, "type": typ, "total_oi": tot, "call_oi": call_at, "put_oi": put_at})

    return {
        "pin": pin,
        "pin_confidence": pin_conf,
        "put_call_ratio": None if pcr is None else round(float(pcr), 2),
        "bias": bias,
        "walls": walls,
    }


# =========================
# Momentum (15m)
# =========================
def fetch_price_history(symbol: str, period_days: int = 5, freq_min: int = 15) -> Optional[pd.DataFrame]:
    url = f"{SCHWAB_API_URL}/pricehistory"
    params = {
        "symbol": symbol,
        "periodType": "day",
        "period": int(period_days),
        "frequencyType": "minute",
        "frequency": int(freq_min),
    }
    headers = get_schwab_headers()
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    logger.info(f"PriceHistory API [{symbol} {freq_min}m] status={resp.status_code}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data or "candles" not in data:
        return None
    df = pd.DataFrame(data["candles"])
    if df.empty or "datetime" not in df.columns:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
    df = df.set_index("datetime")
    df.columns = [c.lower() for c in df.columns]
    need = {"high", "low", "close", "volume"}
    if not need.issubset(set(df.columns)):
        return None
    return df


def compute_momentum(symbol_for_history: str, fallback_price: float) -> Dict[str, Any]:
    """
    Returns: score [0..100], trend: uptrend/downtrend/neutral, volatility: low/medium/high
    """
    df = fetch_price_history(symbol_for_history, period_days=5, freq_min=15)
    if df is None or len(df) < 30 or ta is None:
        return {"score": 50, "trend": "neutral", "volatility": "medium", "atr": 0.015, "rsi": None}

    df = df.copy()
    df.ta.rsi(length=14, append=True)
    df.ta.ema(length=9, append=True)
    df.ta.ema(length=20, append=True)
    df.ta.atr(length=14, append=True)

    score = 50
    trend = "neutral"
    vol = "medium"

    rsi_val = float(df["RSI_14"].iloc[-1]) if "RSI_14" in df.columns and pd.notna(df["RSI_14"].iloc[-1]) else None

    if rsi_val is not None:
        if 40 < rsi_val < 60:
            score += 10
        elif 30 < rsi_val < 70:
            score += 6

    ema9 = float(df["EMA_9"].iloc[-1]) if "EMA_9" in df.columns else None
    ema20 = float(df["EMA_20"].iloc[-1]) if "EMA_20" in df.columns else None
    close = float(df["close"].iloc[-1])

    if ema9 and ema20:
        if ema9 > ema20 and close > ema9:
            trend = "uptrend"
            score += 25
        elif ema9 < ema20 and close < ema9:
            trend = "downtrend"
            score += 25
        else:
            trend = "neutral"
            score += 10

    atr_val = float(df["ATRr_14"].iloc[-1]) if "ATRr_14" in df.columns and pd.notna(df["ATRr_14"].iloc[-1]) else 0.015
    if atr_val > 0.02:
        vol = "high"
        score += 6
    elif atr_val > 0.014:
        vol = "medium"
        score += 3
    else:
        vol = "low"

    score = max(0, min(int(round(score)), 100))
    return {"score": score, "trend": trend, "volatility": vol, "atr": atr_val, "rsi": rsi_val}


# =========================
# Trade planning
# =========================
@dataclass
class TradePlan:
    style: str            # "credit" or "debit"
    action: str           # "SELL" or "BUY"
    side: str             # "call" or "put"
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


def conf_level(score: float) -> str:
    if score >= 85:
        return "HIGH"
    if score >= 70:
        return "MEDIUM-HIGH"
    if score >= 55:
        return "MEDIUM"
    if score >= 40:
        return "MEDIUM-LOW"
    return "LOW"


def _spread_pct(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0:
        return 1.0
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid > 0 else 1.0


def _score_liquidity(row: pd.Series) -> float:
    sp = _spread_pct(float(row["bid"]), float(row["ask"]))
    # 0..1 (tighter is better)
    spread_score = max(0.0, min(1.0, 1.0 - (sp / 0.18)))
    vol_score = min(1.0, float(row.get("volume", 0)) / 500.0)
    oi_score = min(1.0, float(row.get("oi", 0)) / 5000.0)
    return 0.55 * spread_score + 0.25 * vol_score + 0.20 * oi_score


def pick_credit_candidates(
    df: pd.DataFrame,
    underlying: float,
    pin: float,
    strike_step: float,
    momentum: Dict[str, Any],
    min_premium: float,
    max_premium: float,
) -> Dict[str, pd.DataFrame]:
    """
    CREDIT = SELL to open. Return best puts and calls (ranked).
    """
    work = df.copy()

    # Basic sanity
    work = work[(work["bid"] > 0) & (work["ask"] > 0)]
    work["spread_pct"] = work.apply(lambda r: _spread_pct(r["bid"], r["ask"]), axis=1)
    work["abs_delta"] = work["delta"].abs()

    # Filters tuned for 0DTE premium selling
    work = work[
        (work["spread_pct"] <= 0.12)
        & (work["bid"] >= min_premium)
        & (work["bid"] <= max_premium)
        & (work["abs_delta"] >= 0.08)
        & (work["abs_delta"] <= 0.28)
        & ((work["volume"] >= 50) | (work["oi"] >= 200))
    ].copy()

    # Prefer selling away from the pin
    buffer_pts = max(strike_step, round((underlying * 0.003) / strike_step) * strike_step)  # ~0.3%
    # put sells: strike below pin-buffer; call sells: strike above pin+buffer
    work["safe_from_pin"] = work.apply(
        lambda r: (r["strike"] <= (pin - buffer_pts)) if r["side"] == "put" else (r["strike"] >= (pin + buffer_pts)),
        axis=1,
    )
    work = work[work["safe_from_pin"]].copy()

    trend = momentum.get("trend", "neutral")
    # Trend alignment score for credit:
    # uptrend -> prefer selling puts, downtrend -> prefer selling calls
    def _trend_align(side: str) -> float:
        if trend == "uptrend":
            return 1.0 if side == "put" else 0.55
        if trend == "downtrend":
            return 1.0 if side == "call" else 0.55
        return 0.75

    # Distance-from-pin score (0..1)
    work["dist_pin_pts"] = (work["strike"] - pin).abs()
    work["dist_pin_score"] = (work["dist_pin_pts"] / (buffer_pts * 4)).clip(0, 1)

    # IV score (prefer moderate IV for controllable stop)
    work["iv_score"] = ((work["iv"].clip(10, 160) - 10) / 150.0).clip(0, 1)

    # Delta preference (closer to 0.16-0.22 is sweet spot)
    work["delta_score"] = (1.0 - (work["abs_delta"] - 0.20).abs() / 0.20).clip(0, 1)

    work["liq_score"] = work.apply(_score_liquidity, axis=1)
    work["trend_score"] = work["side"].apply(_trend_align)

    # Overall score (0..100-ish)
    work["score"] = (
        35 * work["liq_score"]
        + 25 * work["delta_score"]
        + 20 * work["dist_pin_score"]
        + 10 * work["iv_score"]
        + 10 * work["trend_score"]
    )

    puts = work[work["side"] == "put"].sort_values("score", ascending=False)
    calls = work[work["side"] == "call"].sort_values("score", ascending=False)
    return {"put": puts, "call": calls}


def pick_debit_candidates(
    df: pd.DataFrame,
    momentum: Dict[str, Any],
    min_debit: float,
    max_debit: float,
) -> Dict[str, pd.DataFrame]:
    """
    DEBIT = BUY to open. Return best calls and puts (ranked).
    """
    work = df.copy()
    work = work[(work["bid"] > 0) & (work["ask"] > 0)]
    work["spread_pct"] = work.apply(lambda r: _spread_pct(r["bid"], r["ask"]), axis=1)
    work["abs_delta"] = work["delta"].abs()

    work = work[
        (work["spread_pct"] <= 0.14)
        & (work["ask"] >= min_debit)
        & (work["ask"] <= max_debit)
        & (work["abs_delta"] >= 0.25)
        & (work["abs_delta"] <= 0.62)
        & ((work["volume"] >= 80) | (work["oi"] >= 300))
    ].copy()

    trend = momentum.get("trend", "neutral")

    def _trend_align(side: str) -> float:
        if trend == "uptrend":
            return 1.0 if side == "call" else 0.40
        if trend == "downtrend":
            return 1.0 if side == "put" else 0.40
        return 0.75

    work["liq_score"] = work.apply(_score_liquidity, axis=1)
    work["trend_score"] = work["side"].apply(_trend_align)
    # delta sweet spot ~0.40
    work["delta_score"] = (1.0 - (work["abs_delta"] - 0.40).abs() / 0.30).clip(0, 1)
    # IV sanity: prefer 15..120
    work["iv_score"] = ((work["iv"].clip(10, 160) - 10) / 150.0).clip(0, 1)

    # Overall score
    work["score"] = 40 * work["trend_score"] + 25 * work["liq_score"] + 20 * work["delta_score"] + 15 * work["iv_score"]

    puts = work[work["side"] == "put"].sort_values("score", ascending=False)
    calls = work[work["side"] == "call"].sort_values("score", ascending=False)
    return {"put": puts, "call": calls}


def build_trade_plan(
    row: pd.Series,
    expiration: date,
    style: str,
    max_risk_usd: float,
    contracts_override: Optional[int] = None,
) -> TradePlan:
    """
    Create entry/targets/stop + contracts sizing.
    """
    style = style.lower().strip()
    assert style in {"credit", "debit"}

    side = str(row["side"])
    sym = str(row["symbol"])
    strike = float(row["strike"])

    if style == "credit":
        action = "SELL"
        entry = float(row["bid"])
        target1 = round(entry * 0.90, 2)  # 10% profit (buy back cheaper)
        target2 = round(entry * 0.80, 2)  # 20% profit
        stop = round(entry * 1.60, 2)     # 60% adverse move
        risk_per_contract_pts = max(0.01, stop - entry)
        reward1_pts = max(0.0, entry - target1)
        reward2_pts = max(0.0, entry - target2)
        notes = "Credit (SELL) — manage by premium decay; exit by 3:55 PM ET."
    else:
        action = "BUY"
        entry = float(row["ask"])
        target1 = round(entry * 1.15, 2)  # +15%
        target2 = round(entry * 1.30, 2)  # +30%
        stop = round(entry * 0.60, 2)     # -40%
        risk_per_contract_pts = max(0.01, entry - stop)
        reward1_pts = max(0.0, target1 - entry)
        reward2_pts = max(0.0, target2 - entry)
        notes = "Debit (BUY) — directional; exit by 3:55 PM ET."

    mult = 100.0  # SPX options multiplier
    risk_usd_1 = risk_per_contract_pts * mult
    reward1_usd_1 = reward1_pts * mult
    reward2_usd_1 = reward2_pts * mult

    if contracts_override is not None and contracts_override > 0:
        contracts = int(contracts_override)
    else:
        contracts = max(1, int(max_risk_usd // max(risk_usd_1, 1.0)))

    est_risk = round(risk_usd_1 * contracts, 2)
    est_r1 = round(reward1_usd_1 * contracts, 2)
    est_r2 = round(reward2_usd_1 * contracts, 2)

    score = float(row.get("score", 0.0))
    confidence = int(round(min(100.0, max(0.0, score))))
    level = conf_level(confidence)

    return TradePlan(
        style=style,
        action=action,
        side=side,
        symbol=sym,
        strike=strike,
        expiration=expiration.strftime("%Y-%m-%d"),
        entry=round(entry, 2),
        target1=target1,
        target2=target2,
        stop=stop,
        confidence=confidence,
        confidence_level=level,
        score=round(score, 2),
        contracts=contracts,
        est_risk_usd=est_risk,
        est_reward1_usd=est_r1,
        est_reward2_usd=est_r2,
        notes=notes,
    )


def _print_plan(title: str, plan: TradePlan):
    print("\n" + "=" * 64)
    print(title.center(64))
    print("=" * 64)
    print(f"STYLE:      {plan.style.upper()}   ({plan.action} {plan.side.upper()})")
    print(f"CONTRACT:   {plan.symbol}   STRIKE {plan.strike:.2f}   EXP {plan.expiration}")
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


# =========================
# Main
# =========================
def run(date_str: Optional[str], max_risk: float, contracts: Optional[int], json_out: bool) -> Dict[str, Any]:
    expiration = parse_date_input(date_str)

    df, underlying, raw = fetch_spx_option_chain(expiration)
    step = infer_strike_step(df["strike"])

    pin_info = compute_oi_pin(df, underlying)
    momentum = compute_momentum("$SPX", underlying)

    # Candidate search
    credit = pick_credit_candidates(
        df=df,
        underlying=underlying,
        pin=float(pin_info["pin"]),
        strike_step=step,
        momentum=momentum,
        min_premium=0.25,
        max_premium=25.0,
    )
    debit = pick_debit_candidates(
        df=df,
        momentum=momentum,
        min_debit=1.00,
        max_debit=60.0,
    )

    # Build top plans (best put + best call) for each style
    plans: Dict[str, Optional[TradePlan]] = {"credit_put": None, "credit_call": None, "debit_put": None, "debit_call": None}

    if not credit["put"].empty:
        plans["credit_put"] = build_trade_plan(credit["put"].iloc[0], expiration, "credit", max_risk, contracts)
    if not credit["call"].empty:
        plans["credit_call"] = build_trade_plan(credit["call"].iloc[0], expiration, "credit", max_risk, contracts)
    if not debit["put"].empty:
        plans["debit_put"] = build_trade_plan(debit["put"].iloc[0], expiration, "debit", max_risk, contracts)
    if not debit["call"].empty:
        plans["debit_call"] = build_trade_plan(debit["call"].iloc[0], expiration, "debit", max_risk, contracts)

    out = {
        "symbol": "$SPX",
        "expiration": expiration.strftime("%Y-%m-%d"),
        "underlying": round(float(underlying), 2),
        "strike_step": step,
        "pin": pin_info,
        "momentum": momentum,
        "picks": {k: (asdict(v) if v else None) for k, v in plans.items()},
        "timestamp": datetime.now(EASTERN).isoformat(),
    }

    if json_out:
        print(json.dumps(out, indent=2))
        return out

    print("\n" + "=" * 64)
    print("SPX 0DTE GURU DASHBOARD".center(64))
    print("=" * 64)
    print(f"Symbol:          $SPX")
    print(f"Expiration:      {out['expiration']}")
    print(f"Underlying:      ${out['underlying']:.2f}")
    print(f"Strike step:     {out['strike_step']}")
    print(f"Momentum:        {momentum.get('trend','neutral').upper()} | Score {momentum.get('score',50)}/100 | Vol {momentum.get('volatility','medium')}")
    print(f"OI Pin:          {pin_info['pin']:.2f} | Bias {pin_info['bias']} | PCR {pin_info.get('put_call_ratio')} | PinConf {pin_info['pin_confidence']}")
    print("-" * 64)
    if pin_info.get("walls"):
        print("Top OI walls (nearest first):")
        walls_sorted = sorted(pin_info["walls"], key=lambda w: abs(float(w["strike"]) - underlying))
        for w in walls_sorted[:6]:
            print(f"  - {w['type']:<10} @ {w['strike']:.2f}  (OI {int(w['total_oi']):,})")

    if plans["credit_put"]:
        _print_plan("BEST CREDIT PUT (SELL)", plans["credit_put"])
    if plans["credit_call"]:
        _print_plan("BEST CREDIT CALL (SELL)", plans["credit_call"])
    if plans["debit_call"]:
        _print_plan("BEST DEBIT CALL (BUY)", plans["debit_call"])
    if plans["debit_put"]:
        _print_plan("BEST DEBIT PUT (BUY)", plans["debit_put"])

    if not any(plans.values()):
        print("\n❌ No picks found after filters. Try relaxing thresholds (premium/spread/oi).")

    return out


def main():
    import argparse

    p = argparse.ArgumentParser(description="SPX 0DTE Guru Dashboard (Credit + Debit picks)")
    p.add_argument("--date", default=None, help="today | 0dte | tomorrow | YYYY-MM-DD (ET-based)")
    p.add_argument("--max-risk", type=float, default=100.0, help="Max risk budget in USD (used if --contracts not set)")
    p.add_argument("--contracts", type=int, default=None, help="Override contracts (uses same count for all picks)")
    p.add_argument("--json", action="store_true", help="Output JSON only")
    args = p.parse_args()

    run(date_str=args.date, max_risk=float(args.max_risk), contracts=args.contracts, json_out=bool(args.json))


if __name__ == "__main__":
    main()
