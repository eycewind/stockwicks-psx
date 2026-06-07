# app/utils/scalper_picker.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal, Optional, Dict, Any

from app.utils.option_direction import get_market_bias  # EMA/RSI bias  :contentReference[oaicite:0]{index=0}
from app.utils.option_chain import (
    pick_expiry,                # 0DTE if >=90m left, else next trading day  :contentReference[oaicite:1]{index=1}
    fetch_normalized_chain,     # Schwab fetch → normalized dict               :contentReference[oaicite:2]{index=2}
    filter_liquidity,           # min volume & max spread filters              :contentReference[oaicite:3]{index=3}
)
from app.utils.option_selector import (
    pick_option_selection,      # delta-anchored leg selection                 :contentReference[oaicite:4]{index=4}
    OptionSelection,
)
from app.utils.option_helpers import mark_option  # mid mark helper          :contentReference[oaicite:5]{index=5}


Style = Literal["DEBIT", "CREDIT", "AUTO"]

def pick_scalper_option(
    *,
    user_id: int,
    symbol: str,
    interval: str = "3min",
    style: Style = "AUTO",
    qty: int = 1,
    min_volume: int = 100,           # tighter for scalps
    max_spread_pct: float = 0.15,
    risk_per_trade: float = 250.0,
) -> Optional[Dict[str, Any]]:
    """
    Returns a dict {structure, legs, entry, tp, sl, bias, notes} ready for your bot runner.
    """
    # 1) Fast bias
    bias = get_market_bias(user_id, symbol, interval=interval)   # "bullish" | "bearish" | "neutral"
    # 2) Decide style (AUTO → DEBIT on trend, CREDIT on chop)
    if style == "AUTO":
        style_eff = "DEBIT" if bias in ("bullish", "bearish") else "CREDIT"
    else:
        style_eff = style

    # 3) Expiry & chain (prefer 0DTE if >=90m left)
    expiry = pick_expiry(symbol, prefer_0dte=True, min_minutes_left=90)       # ISO date string  :contentReference[oaicite:6]{index=6}
    chain = fetch_normalized_chain(symbol=symbol, prefer_0dte=True, min_minutes_left=90, explicit_expiry=expiry)
    if not chain:
        return None
    chain = filter_liquidity(chain, expiry, min_volume=min_volume, max_spread_pct=max_spread_pct)

    # 4) Delta-anchored structure selection (debit/credit spreads by bias)
    sel: OptionSelection | None = pick_option_selection(
        symbol=symbol,
        style=style_eff,     # "DEBIT" or "CREDIT" → maps to debit_call/put or bull_put/bear_call  :contentReference[oaicite:7]{index=7}
        bias=bias,
        now=datetime.now(timezone.utc),
        risk_per_trade=risk_per_trade,
        qty=qty,
        chain=chain,
    )
    if sel is None or not sel.legs:
        return None

    # NOTE: option_selector currently stamps legs with today's date internally.
    # We align to the actual picked expiry we used above:
    for leg in sel.legs:
        leg.expiry = expiry  # make it match our liquidity-filtered chain

    # 5) Price the selection using current mids from the chain we already fetched
    book = chain["expiries"][expiry]
    def _row(side: str, strike: float) -> Optional[dict]:
        pool = book["calls" if side == "C" else "puts"]
        for r in pool:
            if abs(float(r["strike"]) - float(strike)) < 1e-6:
                return r
        # fallback: nearest strike
        return min(pool, key=lambda r: abs(float(r["strike"]) - float(strike)), default=None)

    entry = None
    if sel.structure in ("debit_call_spread", "debit_put_spread"):
        # BUY long, SELL short → net debit
        long_leg = next(l for l in sel.legs if l.action == "BUY")
        short_leg = next(l for l in sel.legs if l.action == "SELL")
        long_row = _row(long_leg.right, long_leg.strike)
        short_row = _row(short_leg.right, short_leg.strike)
        if not (long_row and short_row):
            return None
        entry = round(mark_option(long_row) - mark_option(short_row), 2)   #  :contentReference[oaicite:8]{index=8}
        tp = round(entry * 1.30, 2)   # +30% scalp
        sl = round(entry * 0.55, 2)   # -45% stop

    elif sel.structure in ("bull_put_credit", "bear_call_credit"):
        # SELL short, BUY long → net credit
        short_leg = next(l for l in sel.legs if l.action == "SELL")
        long_leg  = next(l for l in sel.legs if l.action == "BUY")
        short_row = _row(short_leg.right, short_leg.strike)
        long_row  = _row(long_leg.right,  long_leg.strike)
        if not (short_row and long_row):
            return None
        entry = round(mark_option(short_row) - mark_option(long_row), 2)   # net credit         :contentReference[oaicite:9]{index=9}
        tp = round(entry * 0.50, 2)   # capture 50% credit
        sl = round(entry * 1.60, 2)   # risk ~1.6x credit

    else:
        # (iron_condor or other structures can be added later)
        return None

    return {
        "symbol": symbol,
        "bias": bias,
        "style": style_eff,
        "structure": sel.structure,
        "expiry": expiry,
        "qty": qty,
        "legs": [l.__dict__ for l in sel.legs],
        "entry": entry,
        "take_profit": tp,
        "stop_loss": sl,
        "notes": sel.notes + f" | liquidity≥{min_volume}, spread≤{int(max_spread_pct*100)}%",
    }
