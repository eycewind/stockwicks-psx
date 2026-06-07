# app/scripts/options/engine_core.py

"""
Core dataclasses and utilities for option strategies.
Provides OptionLeg, Recommendation, and position sizing helpers.
"""

from dataclasses import dataclass
from typing import List, Dict, Optional, Literal
from datetime import datetime

Side = Literal["long", "short"]
CP = Literal["call", "put"]


@dataclass
class OptionLeg:
    type: CP            # "call" | "put"
    side: Side          # "long" | "short"
    strike: float
    expiry: datetime
    mid: float
    delta: float
    gamma: float
    vega: float
    theta: float
    iv: float
    occ: Optional[str] = None  # OCC symbol if available


@dataclass
class Recommendation:
    strategy: Literal["debit", "credit_spread"]
    symbol: str
    legs: List[OptionLeg]
    entry_net_price: float          # debit paid (>0) or credit received (>0)
    stop_loss: Optional[float]
    take_profit: Optional[float]
    rationale: str
    meta: Dict


# ---------------------------
# Core helpers
# ---------------------------

def net_price_of_legs(legs: List[OptionLeg]) -> float:
    """
    Compute net debit/credit of a strategy given its legs.
    Long = pay, Short = receive.
    """
    total = 0.0
    for leg in legs:
        sign = 1 if leg.side == "long" else -1
        total += sign * leg.mid
    return round(total, 2)


def calc_position_size(account_equity: float,
                       risk_pct: float,
                       unit_risk: float,
                       min_qty: int = 1) -> int:
    """
    Position sizing: risk-based allocation.
    account_equity: total paper equity
    risk_pct: % of equity to risk per trade (e.g., 1.0 for 1%)
    unit_risk: risk of one contract
    """
    if unit_risk <= 0:
        return 0
    qty = int((account_equity * (risk_pct / 100.0)) // unit_risk)
    return max(qty, min_qty)
