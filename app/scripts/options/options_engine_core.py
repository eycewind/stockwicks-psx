# app/scripts/options_engine_core.py
from dataclasses import dataclass
from typing import List, Dict, Optional, Literal
from datetime import datetime
from app.utils.options.options_data import fetch_option_chain

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

def get_option_universe(symbol: str,
                        dte_min: int,
                        dte_max: int,
                        min_oi: int,
                        min_vol: int) -> Dict[datetime, Dict]:
    return fetch_option_chain(symbol, dte_min, dte_max, min_oi, min_vol)

def net_price_of_legs(legs: List[OptionLeg]) -> float:
    total = 0.0
    for leg in legs:
        sign = 1 if leg.side == "long" else -1
        total += sign * leg.mid
    return abs(total)

def calc_position_size(account_equity: float,
                       risk_pct: float,
                       unit_risk: float,
                       min_qty: int = 1) -> int:
    if unit_risk <= 0:
        return 0
    qty = int((account_equity * (risk_pct/100.0)) // unit_risk)
    return max(qty, 0)
