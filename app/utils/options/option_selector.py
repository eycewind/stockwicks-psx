# app/utils/option_selector.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional, List, Dict
from datetime import datetime, timedelta

Style = Literal["DEBIT", "CREDIT"]
Bias  = Literal["bullish", "bearish", "neutral"]

@dataclass
class OptionLeg:
    right: Literal["C", "P"]     # call/put
    strike: float
    expiry: str                  # ISO date 'YYYY-MM-DD'
    action: Literal["BUY", "SELL"]
    qty: int

@dataclass
class OptionSelection:
    structure: str               # 'debit_call_spread', 'bear_call_credit', etc.
    legs: List[OptionLeg]
    target_debit: Optional[float] = None
    target_credit: Optional[float] = None
    notes: str = ""

def _days_to_expiry(now: datetime) -> str:
    # choose 0DTE if >90min left, else 1DTE
    # keep simple; refine later by session clock you already track
    e = now.date()
    return e.isoformat()

def pick_structure(style: Style, bias: Bias) -> str:
    if style == "DEBIT":
        if bias == "bullish":
            return "debit_call_spread"
        if bias == "bearish":
            return "debit_put_spread"
        return "calendar_neutral"
    else:  # CREDIT
        if bias == "bullish":
            return "bull_put_credit"
        if bias == "bearish":
            return "bear_call_credit"
        return "iron_condor"

def _target_deltas(structure: str) -> Dict[str, float]:
    """
    Delta anchors for each leg.
    """
    m = {
        "debit_call_spread":  {"long": 0.40, "short": 0.55},
        "debit_put_spread":   {"long": -0.40, "short": -0.55},
        "bull_put_credit":    {"short": -0.25, "long": -0.15},
        "bear_call_credit":   {"short": 0.25,  "long":  0.15},
        "iron_condor":        {"put_short": -0.20, "put_long": -0.10, "call_short": 0.20, "call_long": 0.10},
        "calendar_neutral":   {"long": 0.35},  # placeholder; needs two expiries
    }
    return m.get(structure, {})

def _choose_strikes_by_delta(chain: dict, right: str, expiry: str, target_delta: float) -> Optional[float]:
    """
    chain format expectation:
      chain["expiries"][expiry]["calls"/"puts"] = list of { "strike": float, "delta": float, ... }
    You already have Schwab auth; we’ll wire the real fetch next.
    """
    side_key = "calls" if right == "C" else "puts"
    rows = chain["expiries"].get(expiry, {}).get(side_key, [])
    if not rows:
        return None
    # pick closest to target delta by absolute diff
    chosen = min(rows, key=lambda r: abs(float(r.get("delta", 0.0)) - target_delta))
    return float(chosen["strike"])

def pick_option_selection(
    *,
    symbol: str,
    style: Style,
    bias: Bias,
    now: datetime,
    risk_per_trade: float,
    qty: int,
    chain: dict,  # pre-fetched option chain for symbol
) -> Optional[OptionSelection]:
    structure = pick_structure(style, bias)

    # choose expiry
    expiry = _days_to_expiry(now)
    d = _target_deltas(structure)

    legs: List[OptionLeg] = []
    notes = f"{structure} via delta anchors {d}"

    def add_leg(action: str, right: str, strike: float) -> None:
        legs.append(OptionLeg(right=right, strike=round(strike, 2), expiry=expiry, action=action, qty=qty))

    if structure == "debit_call_spread":
        k_long  = _choose_strikes_by_delta(chain, "C", expiry, d["long"])
        k_short = _choose_strikes_by_delta(chain, "C", expiry, d["short"])
        if not (k_long and k_short and k_short > k_long):
            return None
        add_leg("BUY",  "C", k_long)
        add_leg("SELL", "C", k_short)
        return OptionSelection(structure=structure, legs=legs, notes=notes)

    if structure == "debit_put_spread":
        k_long  = _choose_strikes_by_delta(chain, "P", expiry, d["long"])
        k_short = _choose_strikes_by_delta(chain, "P", expiry, d["short"])
        if not (k_long and k_short and k_short < k_long):
            return None
        add_leg("BUY",  "P", k_long)
        add_leg("SELL", "P", k_short)
        return OptionSelection(structure=structure, legs=legs, notes=notes)

    if structure == "bull_put_credit":
        k_short = _choose_strikes_by_delta(chain, "P", expiry, d["short"])
        k_long  = _choose_strikes_by_delta(chain, "P", expiry, d["long"])
        if not (k_short and k_long and k_long < k_short):
            return None
        add_leg("SELL", "P", k_short)
        add_leg("BUY",  "P", k_long)
        return OptionSelection(structure=structure, legs=legs, notes=notes)

    if structure == "bear_call_credit":
        k_short = _choose_strikes_by_delta(chain, "C", expiry, d["short"])
        k_long  = _choose_strikes_by_delta(chain, "C", expiry, d["long"])
        if not (k_short and k_long and k_long > k_short):
            return None
        add_leg("SELL", "C", k_short)
        add_leg("BUY",  "C", k_long)
        return OptionSelection(structure=structure, legs=legs, notes=notes)

    if structure == "iron_condor":
        ks = _choose_strikes_by_delta(chain, "P", expiry, d["put_short"])
        kl = _choose_strikes_by_delta(chain, "P", expiry, d["put_long"])
        cs = _choose_strikes_by_delta(chain, "C", expiry, d["call_short"])
        cl = _choose_strikes_by_delta(chain, "C", expiry, d["call_long"])
        if not all([ks, kl, cs, cl]) or not (kl < ks < cs < cl):
            return None
        add_leg("SELL", "P", ks); add_leg("BUY", "P", kl)
        add_leg("SELL", "C", cs); add_leg("BUY", "C", cl)
        return OptionSelection(structure=structure, legs=legs, notes=notes)

    # calendar_neutral and others: add later
    return None
