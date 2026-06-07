#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/options/spx_0dte_bot_runner.py
"""
SPX 0DTE Paper Bot — v4.0 /ES MOMENTUM + DECAY-AWARE OPTION SELECTION
Buy-only (long options)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v4.0 — FUNDAMENTAL REWRITE OF SELECTION + ENTRY LOGIC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WHY v4 EXISTS:
  Buyer logic (v1-v3) chased SMI reversals and selected near-ATM options
  by distance scoring. Every loss followed the same pattern: signal
  fired, theta ate $30-50, then a normal /ES wiggle stopped us out.
  Theta + slippage > our edge from the SMI signal.

WHAT'S NEW:

1. /ES IS THE PRIMARY SIGNAL (SMI demoted to tiebreaker)
   • Range-expansion vs contraction over last 6 bars
   • VWAP relationship + slope on 1-min /ES
   • 5-min higher-highs / lower-lows structural read
   • Combined into "edge_state": STRONG_TREND / WEAK_TREND / CHOP

2. DECAY-AWARE OPTION SELECTION
   • Reads option theta from chain (Schwab provides it, we ignored it)
   • Computes theta cost over expected hold time
   • Calculates breakeven /ES move: needed_move = (theta_cost +
     target_profit + slippage) / (delta * 100)
   • REJECTS option if needed_move > 1.5x recent /ES velocity
   • Picks strike that maximizes (delta × expected_move - theta_cost)

3. TIME-OF-DAY AWARE
   • Pre-11:00:  buy any qualifying option (theta moderate)
   • 11:00-13:30: only ITM-leaning (delta > 0.35) — theta brutal on OTM
   • Post-13:30:  only deep-ITM (delta > 0.50) or skip
   • Post-15:00:  no new entries (gamma risk)

4. STRIKE TARGETING SHIFTED
   • Old:  delta 0.25-0.55 (too wide, too much gamma risk)
   • New:  delta 0.22-0.40 (sweet spot for /ES move capture)
   • New:  filter theta_pct_of_premium <= 8% per 10 min

5. RETAINED FROM PRIOR VERSIONS
   • TRAIL_GAP_POINTS = 0.40, ARM at +$0.50 (tight trail v2.2)
   • TP1 slippage tolerance = 0.95 (let TPs fire properly)
   • Risk-based stop = $100/contract max
   • Daily loss cap, cooldowns, max trades/day
   • Option chain fetching, mark/exit price helpers (unchanged)

DEMOTED BUT STILL USED:
  • SMI is computed but only acts as a tiebreaker when /ES signal
    is borderline. It cannot generate a trade alone.
  • Old SMI gates (overbought/oversold cross) are completely removed.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, date, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, Tuple, List

import numpy as np
import pandas as pd
import pytz
import requests

_THIS_FILE = os.path.abspath(__file__)
_THIS_DIR = os.path.dirname(_THIS_FILE)
PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "../../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.database.connection import SessionLocal
from app.models.paper_spx_0dte import PaperSPXPick, PaperSPXOpenTrade, PaperSPXTradeHistory
from app.models.spx_0dte_alert_subscription import SPX0DTEAlertSubscription
from app.services.email_service import EmailService, notification_already_sent, log_notification

ET = pytz.timezone("US/Eastern")
UTC = pytz.utc

TEST_AFTER_HOURS = os.getenv("SPX0DTE_TEST_AFTER_HOURS", "0").strip().lower() in ("1", "true", "yes", "y")

SPX_CANONICAL = "SPX"
SPX_SCHWAB = "$SPX"
SPX_WEEKLY = "SPXW"
ES_FUTURES = "/ESH26"

# ─── SMI Parameters ──────────────────────────────────────────────────────────
SMI_PERCENT_K = 5
SMI_PERCENT_D = 3
SMI_OVER_BOUGHT = 40.0
SMI_OVER_SOLD = -40.0
SMI_MOMENTUM_MIN_MOVE = 5.0   # fast line must move >= 5pts to qualify

REVERSAL_LOOKBACK_CANDLES = 8
STRICT_LEVEL_CROSS = True

# ─── Chart Intervals ─────────────────────────────────────────────────────────
CHART_INTERVAL_MINUTES = int(os.getenv("SPX0DTE_CHART_INTERVAL", "1"))
CONFIRM_INTERVAL_MINUTES = 5

# ─── /ES Momentum Reading (NEW IN v4) ────────────────────────────────────────
# Range expansion: current bar range / mean of last N bar ranges
RANGE_EXPANSION_LOOKBACK = 6
RANGE_EXPANSION_THRESHOLD = 1.30    # current range >= 1.3x avg = expanding

# Velocity = average abs price change per bar over recent window
VELOCITY_WINDOW_BARS = 8            # measure velocity over last 8 minutes
MIN_ES_VELOCITY = 0.30              # /ES must move >= 0.30 pts/min avg to trade

# VWAP slope check: positive over how many bars = uptrend confirmed
VWAP_SLOPE_BARS = 5
VWAP_SLOPE_MIN = 0.05               # vwap rising/falling at least 0.05/bar

# Structural read on 5-min bars
STRUCTURAL_BARS_5M = 6              # last 6 5-min bars (30 min of context)

# ─── Option Filters (TIGHTENED for v4 decay-aware) ───────────────────────────
MAX_PREMIUM = 12.00      # was 20 — limits absolute dollar swings
MIN_PREMIUM = 0.5       # was 2.00 — slip-resistant
MIN_BID = 0.45           # was 0.50 — real liquidity
MAX_SPREAD = 0.0        # was 1.50 — wide spreads kill scalpers
MIN_VOLUME = 10          # was 5
MIN_OPEN_INTEREST = 0

# Delta sweet spot: far enough OTM for cheap entry, close enough for gamma
MIN_DELTA = 0.10         # was 0.25
MAX_DELTA = 0.75         # was 0.55 — high gamma = high risk

# Time-of-day delta requirements (NEW IN v4)
# Theta accelerates through the day. ITM-leaning options decay slower
# as percentage of premium, so we require deeper delta later in the day.
# DELTA_FLOOR_BY_HOUR = {
#     # hour_et: minimum_abs_delta_required
#     9:  0.22,
#     10: 0.22,
#     11: 0.30,   # transition starts
#     12: 0.35,
#     13: 0.35,
#     14: 0.40,   # only ITM-leaning after 2 PM
#     15: 0.50,   # only deep-ITM after 3 PM (or skip)
# }

# # Decay filter: theta as % of premium per 10 min must be below this
# MAX_THETA_PCT_PER_10MIN = 0.10  # 10% of premium per 10 min = too fast decay
# ─── Aggressive Day-Trade Delta Floor ────────────────────────────────────────
# Delta is NOT decay; theta is decay.
# Delta only controls how responsive the option is to SPX movement.
# For aggressive 0DTE scalping, allow cheaper/lower-delta contracts too.
DELTA_FLOOR_BY_HOUR = {
    9:  0.15,
    10: 0.15,
    11: 0.18,
    12: 0.18,
    13: 0.20,
    14: 0.20,
    15: 0.22,
}

# Decay filter: theta as % of premium per 10 min.
# Aggressive setting allows faster-decaying contracts if the move/edge is strong.
MAX_THETA_PCT_PER_10MIN = 0.18

# ─── Trade Management ─────────────────────────────────────────────────────────
# Tightened from v2.1 — small wins were getting trailed back to entry
ARM_PROFIT_POINTS = 0.50    # was 1.00 — arm trail at +$50 profit
TRAIL_GAP_POINTS = 0.40     # was 1.00 — was killing winners

# TP — taken seriously; this is where we make our money
TAKE_PROFIT_POINTS = 1.50   # +$150/contract target
TP_SLIP_TOLERANCE = 0.95    # was 0.90 — let TP fire even on small slip

# Lock-in: when mark hits +$1.00, raise stop to lock +$0.30 minimum
LOCK_IN_TRIGGER_POINTS = 1.00
LOCK_IN_FLOOR_POINTS = 0.30

# Time stop: if flat after this long, exit before theta destroys us
TIME_STOP_MINUTES = 8
TIME_STOP_MIN_PROFIT = 0.20  # need at least +$0.20 to stay in

RISK_PER_TRADE = 100.0      # max realized loss per contract

# ─── Edge Quality Gates (NEW IN v4) ──────────────────────────────────────────
# Trade only fires when expected /ES move > 1.5x needed move to break even
EDGE_RATIO_MIN = 1.50

# Edge state required (computed from /ES read)
# STRONG_TREND: clear directional bias, expanding range
# WEAK_TREND:   leaning one way but mixed signals
# CHOP:         sideways, contracting range — no buying allowed
ALLOWED_EDGE_STATES = {"STRONG_TREND"}  # tightest: only strong trends
# To loosen later: ALLOWED_EDGE_STATES = {"STRONG_TREND", "WEAK_TREND"}

# ─── Risk Controls ────────────────────────────────────────────────────────────
MIN_CONFIDENCE_FOR_TRADE = 60
MAX_DAILY_LOSS = 400                   # was 500 — tightened
MAX_CONSECUTIVE_LOSSES = 5             # was 3 — stop sooner
MAX_TRADES_PER_DAY = 6                 # was 8 — quality > quantity
COOLDOWN_AFTER_LOSS_MINUTES = 15       # was 10
COOLDOWN_AFTER_SIGNAL_MINUTES = 5

# ─── Time Windows ─────────────────────────────────────────────────────────────
OPEN_WINDOW = (9, 45, 16, 0)           # was 15:30 — exit even earlier
AVOID_WINDOWS = [
    (15, 59, 16, 15),                  # lunch chop
    (15, 59, 16, 0),                   # pre-EOD gamma
]
NEW_ENTRY_CUTOFF_TIME = dtime(16, 0)   # NEW: no new trades after 3 PM
EOD_EXIT_TIME = dtime(15, 58)

log = logging.getLogger("SPX0DTE")
if not log.handlers:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)



def _spx_alert_interval_label() -> str:
    return f"{int(CHART_INTERVAL_MINUTES)}min"


def _maybe_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _history_stop_target(history_trade: PaperSPXTradeHistory) -> tuple[Optional[float], Optional[float]]:
    stop_loss = _maybe_float(getattr(history_trade, "planned_stop_loss", None))
    profit_target = _maybe_float(getattr(history_trade, "planned_take_profit_1", None))
    if stop_loss is not None or profit_target is not None:
        return stop_loss, profit_target
    try:
        details = getattr(history_trade, "details_json", None)
        if details:
            d = json.loads(details)
            stop_loss = _maybe_float(d.get("planned_stop_loss"))
            profit_target = _maybe_float(d.get("planned_take_profit_1"))
    except Exception:
        pass
    return stop_loss, profit_target


def _send_spx_open_alerts(open_trade: PaperSPXOpenTrade) -> None:
    ndb = SessionLocal()
    try:
        subscribers = (
            ndb.query(SPX0DTEAlertSubscription)
            .filter(SPX0DTEAlertSubscription.is_active == True)
            .all()
        )
        if not subscribers:
            return

        mailer = EmailService()
        for sub in subscribers:
            email = str(getattr(sub, "email", "") or "").strip()
            if not email:
                continue

            if notification_already_sent(
                ndb,
                user_id=int(sub.user_id),
                notification_type="SPX0DTE_OPEN",
                trade_id=getattr(open_trade, "id", None),
                trade_type="spx0dte",
            ):
                continue

            try:
                stop_loss = _maybe_float(getattr(open_trade, "planned_stop_loss", None))
                profit_target = _maybe_float(getattr(open_trade, "planned_take_profit_1", None))
                mailer.send_option_trade_notification(
                    email=email,
                    symbol=getattr(open_trade, "underlying_symbol", None) or SPX_CANONICAL,
                    side="BUY",
                    price=float(getattr(open_trade, "entry_price", 0.0) or 0.0),
                    qty=int(getattr(open_trade, "quantity", 1) or 1),
                    interval=_spx_alert_interval_label(),
                    algo_name="SPX 0DTE Bot",
                    strike=float(getattr(open_trade, "strike", 0.0) or 0.0),
                    expiry=getattr(open_trade, "expiration", None),
                    position_side=getattr(open_trade, "put_call", None) or "OPTION",
                    stop_loss=stop_loss,
                    profit_target=profit_target,
                )
                log_notification(
                    ndb,
                    user_id=int(sub.user_id),
                    notification_type="SPX0DTE_OPEN",
                    trade_id=getattr(open_trade, "id", None),
                    email_to=email,
                    payload={
                        "occ_symbol": getattr(open_trade, "occ_symbol", None),
                        "stop_loss": stop_loss,
                        "profit_target": profit_target,
                    },
                    trade_type="spx0dte",
                )
            except Exception as e:
                log.warning("[SPX0DTE] Open alert failed for subscriber=%s email=%s err=%s", getattr(sub, "user_id", None), email, e)
    finally:
        ndb.close()


def _send_spx_close_alerts(history_trade: PaperSPXTradeHistory) -> None:
    ndb = SessionLocal()
    try:
        subscribers = (
            ndb.query(SPX0DTEAlertSubscription)
            .filter(SPX0DTEAlertSubscription.is_active == True)
            .all()
        )
        if not subscribers:
            return

        mailer = EmailService()
        stop_loss, profit_target = _history_stop_target(history_trade)
        for sub in subscribers:
            email = str(getattr(sub, "email", "") or "").strip()
            if not email:
                continue

            if notification_already_sent(
                ndb,
                user_id=int(sub.user_id),
                notification_type="SPX0DTE_CLOSE",
                trade_id=getattr(history_trade, "id", None),
                trade_type="spx0dte",
            ):
                continue

            try:
                mailer.send_option_trade_notification(
                    email=email,
                    symbol=getattr(history_trade, "underlying_symbol", None) or SPX_CANONICAL,
                    side="SELL",
                    price=float(getattr(history_trade, "exit_price", 0.0) or 0.0),
                    qty=int(getattr(history_trade, "quantity", 1) or 1),
                    interval=_spx_alert_interval_label(),
                    algo_name="SPX 0DTE Bot",
                    strike=float(getattr(history_trade, "strike", 0.0) or 0.0),
                    expiry=getattr(history_trade, "expiration", None),
                    position_side=getattr(history_trade, "put_call", None) or "OPTION",
                    pnl=float(getattr(history_trade, "pnl_usd", 0.0) or 0.0),
                    stop_loss=stop_loss,
                    profit_target=profit_target,
                )
                log_notification(
                    ndb,
                    user_id=int(sub.user_id),
                    notification_type="SPX0DTE_CLOSE",
                    trade_id=getattr(history_trade, "id", None),
                    email_to=email,
                    payload={
                        "occ_symbol": getattr(history_trade, "occ_symbol", None),
                        "stop_loss": stop_loss,
                        "profit_target": profit_target,
                    },
                    trade_type="spx0dte",
                )
            except Exception as e:
                log.warning("[SPX0DTE] Close alert failed for subscriber=%s email=%s err=%s", getattr(sub, "user_id", None), email, e)
    finally:
        ndb.close()

# ─── Logging ──────────────────────────────────────────────────────────────────

def _ensure_file_logger(user_id: Optional[int] = None) -> None:
    try:
        for h in log.handlers:
            if isinstance(h, RotatingFileHandler):
                return
        preferred = None
        if user_id:
            preferred = f"/var/www/stockwicks/data/{int(user_id)}/spx0dte_bot.log"
        fallback = "/var/www/stockwicks/logs/spx0dte_bot.log"
        path = preferred if preferred and os.path.isdir(os.path.dirname(preferred)) else fallback
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        log.addHandler(fh)
        log.info("[SPX0DTE] File logging enabled -> %s (level=DEBUG)", path)
    except Exception as e:
        log.warning("[SPX0DTE] Could not enable file logging: %s", e)




def _spx0dte_effective_user_id(user_id: Optional[int] = None) -> int:
    try:
        return int(user_id or os.getenv("SPX0DTE_USER_ID", "116") or 116)
    except Exception:
        return 116


def _spx0dte_status_path(user_id: Optional[int] = None) -> str:
    uid = _spx0dte_effective_user_id(user_id)
    return f"/var/www/stockwicks/data/{uid}/spx0dte_status.jsonl"


def _spx0dte_trend_label(signal: Optional[str], data: Optional[Dict[str, Any]] = None) -> str:
    """User-facing trend label for the dashboard."""
    sig = str(signal or "").upper()
    if sig == "CALL":
        return "UP"
    if sig == "PUT":
        return "DOWN"

    d = data or {}
    reason = str(d.get("reason") or "").upper()
    if "CALL" in reason or str(d.get("price_vs_vwap") or "").upper() == "ABOVE":
        return "UP"
    if "PUT" in reason or str(d.get("price_vs_vwap") or "").upper() == "BELOW":
        return "DOWN"
    return "NEUTRAL"


def _write_spx0dte_status(
    user_id: Optional[int],
    *,
    action: str,
    reason: str = "",
    signal: str = "NONE",
    es_data: Optional[Dict[str, Any]] = None,
    risk: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Append one compact bot-status row per run.

    Dashboard reads the last 5 rows from:
      /var/www/stockwicks/data/<user_id>/spx0dte_status.jsonl
    """
    try:
        es_data = es_data or {}
        risk = risk or {}
        extra = extra or {}
        uid = _spx0dte_effective_user_id(user_id)
        path = _spx0dte_status_path(uid)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        row = {
            "ts_et": _now_et().isoformat(),
            "user_id": uid,
            "bot": "SPX0DTE",
            "status": "RUNNING",
            "action": str(action or "UNKNOWN").upper(),
            "signal": str(signal or es_data.get("signal") or "NONE").upper(),
            "trend": _spx0dte_trend_label(signal or es_data.get("signal"), es_data),
            "reason": reason or es_data.get("reason") or "",
            "edge_state": es_data.get("edge_state"),
            "velocity": es_data.get("velocity"),
            "expansion_ratio": es_data.get("expansion_ratio"),
            "price_vs_vwap": es_data.get("price_vs_vwap"),
            "vwap_slope": es_data.get("vwap_slope"),
            "structure_5m": es_data.get("structure_5m"),
            "expected_move_5min": es_data.get("expected_move_5min"),
            "smi_fast_prev": es_data.get("fast_prev"),
            "smi_fast_curr": es_data.get("fast_curr"),
            "last_bar_time": es_data.get("last_bar_time"),
            "risk_can_trade": risk.get("can_trade"),
            "daily_pnl": risk.get("daily_pnl"),
            "trades_today": risk.get("trades_today") or risk.get("trades"),
            "consecutive_losses": risk.get("consecutive_losses") or risk.get("consec_losses"),
            **extra,
        }

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

        # Keep the status file small and fast to read.
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            max_rows = int(os.getenv("SPX0DTE_STATUS_MAX_ROWS", "300"))
            if len(lines) > max_rows:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-max_rows:])
        except Exception:
            pass

    except Exception as e:
        log.warning("[SPX0DTE] Could not write status jsonl: %s", e)

# ─── Utilities ────────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(ET)


def _today_et() -> date:
    return _now_et().date()


def _safe_varchar(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:max_len]


def _forced_run_date() -> Optional[date]:
    s = os.getenv("SPX0DTE_FORCE_RUN_DATE", "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _is_weekday(now_et: Optional[datetime] = None) -> bool:
    return (now_et or _now_et()).weekday() < 5


def _is_avoid_window(now_et: Optional[datetime] = None) -> bool:
    now_et = now_et or _now_et()
    t = now_et.time()
    for sh, sm, eh, em in AVOID_WINDOWS:
        if dtime(sh, sm) <= t <= dtime(eh, em):
            return True
    return False


def _is_open_window(now_et: Optional[datetime] = None) -> bool:
    now_et = now_et or _now_et()
    if not _is_weekday(now_et):
        return False
    t = now_et.time()
    sh, sm, eh, em = OPEN_WINDOW
    return dtime(sh, sm) <= t <= dtime(eh, em) and not _is_avoid_window(now_et)


def _normalize_occ(s: str) -> str:
    return str(s or "").replace(" ", "").strip()


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _as_float(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _get_schwab_headers() -> Dict[str, str]:
    from app.utils.stock.schwab_token import get_valid_access_token
    token = get_valid_access_token()
    if not token:
        raise RuntimeError("No valid Schwab access token")
    return {"Authorization": f"Bearer {token}"}


# ─── Option Chain Fetching ────────────────────────────────────────────────────

def _fetch_option_chain_native(symbol: str, expiration_date: date) -> Tuple[pd.DataFrame, Optional[float]]:
    url = f"{os.getenv('SCHWAB_API_URL', 'https://api.schwabapi.com/marketdata/v1').rstrip('/')}/chains"
    params = {
        "symbol": symbol,
        "fromDate": expiration_date.strftime("%Y-%m-%d"),
        "toDate": expiration_date.strftime("%Y-%m-%d"),
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "range": "ALL",
    }

    log.debug("[SPX0DTE] Fetching option chain | symbol=%s exp=%s", symbol, expiration_date)

    try:
        headers = _get_schwab_headers()
        r = requests.get(url, headers=headers, params=params, timeout=20)

        if r.status_code != 200:
            log.warning("[SPX0DTE] Chain API failed | symbol=%s status=%s body=%s",
                        symbol, r.status_code, r.text[:400])
            return pd.DataFrame(), None

        js = r.json() if r.content else {}
        uq = js.get("underlying", {}) or {}
        qq = js.get("underlyingQuote", {}) or {}
        underlying = (
            uq.get("last") or qq.get("lastPrice") or qq.get("mark")
            or qq.get("askPrice") or qq.get("bidPrice")
        )

        rows: List[Dict[str, Any]] = []

        def _collect(side_key: str, side_name: str):
            exp_map = js.get(side_key, {}) or {}
            for exp_key, strikes in exp_map.items():
                for strike_key, contracts in (strikes or {}).items():
                    for c in (contracts or []):
                        rows.append({
                            "symbol": str(c.get("symbol", "")).replace(" ", "").strip(),
                            "strike": _as_float(c.get("strikePrice"), 0.0),
                            "putCall": str(c.get("putCall", side_name)).upper(),
                            "bid": _as_float(c.get("bid"), 0.0),
                            "ask": _as_float(c.get("ask"), 0.0),
                            "mark": _as_float(c.get("mark"), 0.0),
                            "last": _as_float(c.get("last"), 0.0),
                            "openInterest": _as_float(c.get("openInterest"), 0.0),
                            "totalVolume": _as_float(c.get("totalVolume"), 0.0),
                            "delta": _as_float(c.get("delta"), 0.0),
                            "theta": _as_float(c.get("theta"), 0.0),
                            "gamma": _as_float(c.get("gamma"), 0.0),
                            "vega": _as_float(c.get("vega"), 0.0),
                            "volatility": _as_float(c.get("volatility"), 0.0),
                            "expKey": exp_key,
                            "strikeKey": strike_key,
                        })

        _collect("callExpDateMap", "CALL")
        _collect("putExpDateMap", "PUT")

        df = pd.DataFrame(rows)
        log.info("[SPX0DTE] Chain fetch | symbol=%s rows=%d underlying=%s", symbol, len(df), underlying)
        return df, (float(underlying) if underlying is not None else None)

    except Exception as e:
        log.exception("[SPX0DTE] Chain fetch error | symbol=%s err=%s", symbol, e)
        return pd.DataFrame(), None


def _fetch_full_option_chain_spx(exp_date: date) -> Tuple[Optional[pd.DataFrame], Optional[float], Optional[str]]:
    roots = [SPX_WEEKLY, SPX_SCHWAB, SPX_CANONICAL]
    today = _today_et()
    if exp_date != today:
        log.warning("[SPX0DTE] Exp %s is not today %s — 0DTE only", exp_date, today)
        return None, None, None

    for root in roots:
        df, px = _fetch_option_chain_native(root, exp_date)
        if df is not None and not df.empty:
            log.info("[SPX0DTE] Chain fetched | root=%s rows=%d", root, len(df))
            return df, px, root
        log.warning("[SPX0DTE] No data from root=%s", root)

    return None, None, None


# ─── Price History ────────────────────────────────────────────────────────────

def _parse_schwab_candle_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "datetime" not in out.columns:
        raise ValueError("Schwab candle data missing 'datetime' field")
    out["datetime"] = pd.to_datetime(out["datetime"], unit="ms", utc=True, errors="coerce")
    out = out.dropna(subset=["datetime"]).copy()
    out["datetime"] = out["datetime"].dt.tz_convert(ET)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["high", "low", "close"]).copy()
    out = out.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
    return out


def _filter_regular_session_completed_bars(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    t = out["datetime"].dt.time
    out = out[(t >= dtime(9, 30)) & (t <= dtime(16, 0))].copy()
    now_et = _now_et()
    current_minute_floor = now_et.replace(second=0, microsecond=0)
    out = out[out["datetime"] < current_minute_floor].copy()
    return out.sort_values("datetime").reset_index(drop=True)


def _fetch_futures_history_best_effort(minutes: int = 240, interval_minutes: int = 1) -> Optional[pd.DataFrame]:
    log.info("[SPX0DTE] Fetching %s history | interval=%dmin", ES_FUTURES, interval_minutes)

    try:
        headers = _get_schwab_headers()
        symbols_to_try = [ES_FUTURES, "/ES", "ES", "ESH26", "./ESH26"]
        base = os.getenv("SCHWAB_API_URL", "https://api.schwabapi.com/marketdata/v1").rstrip("/")
        url = f"{base}/pricehistory"

        now_utc = datetime.now(UTC)
        bars_needed = max(minutes + 90, 360)
        start_utc = now_utc - timedelta(minutes=bars_needed * interval_minutes)
        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(now_utc.timestamp() * 1000)

        for sym in symbols_to_try:
            params = {
                "symbol": sym,
                "periodType": "day",
                "period": 1,
                "frequencyType": "minute",
                "frequency": interval_minutes,
                "needExtendedHoursData": "false",
                "startDate": start_ms,
                "endDate": end_ms,
            }
            try:
                r = requests.get(url, headers=headers, params=params, timeout=20)
                if r.status_code != 200:
                    log.warning("[SPX0DTE] pricehistory non-200 for %s: %s", sym, r.status_code)
                    continue

                js = r.json() if r.content else {}
                candles = js.get("candles") or []
                if not candles:
                    continue

                raw = pd.DataFrame(candles)
                need_cols = {"datetime", "high", "low", "close"}
                if not need_cols.issubset(raw.columns):
                    continue

                df = _parse_schwab_candle_datetime(raw)
                df = _filter_regular_session_completed_bars(df)
                if len(df) < 20:
                    continue

                rows_needed = max(120, int(minutes) + 30)
                out = df.tail(rows_needed).reset_index(drop=True)
                log.info("[SPX0DTE] Got %d completed %dmin candles from %s", len(out), interval_minutes, sym)
                return out

            except Exception as e:
                log.warning("[SPX0DTE] History fetch failed for %s: %s", sym, e)
                continue

    except Exception as e:
        log.error("[SPX0DTE] Price history fetch failed: %s", e)

    return None


# ─── SMI Calculation ──────────────────────────────────────────────────────────

def compute_smi(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values("datetime").reset_index(drop=True)
    log.debug("[SPX0DTE] Computing SMI on %d candles", len(out))

    min_low = out["low"].rolling(window=SMI_PERCENT_K, min_periods=SMI_PERCENT_K).min()
    max_high = out["high"].rolling(window=SMI_PERCENT_K, min_periods=SMI_PERCENT_K).max()
    rel_diff = out["close"] - ((max_high + min_low) / 2.0)
    diff = max_high - min_low

    avgrel = _ema(_ema(rel_diff, SMI_PERCENT_D), SMI_PERCENT_D)
    avgdiff = _ema(_ema(diff, SMI_PERCENT_D), SMI_PERCENT_D)

    smi = np.where(avgdiff != 0, avgrel / (avgdiff / 2.0) * 100.0, np.nan)
    out["smi_fast"] = pd.Series(smi, index=out.index, dtype="float64")
    out["smi_slow"] = _ema(out["smi_fast"], SMI_PERCENT_D)

    return out


def _compute_ema20_trend(df: pd.DataFrame) -> Optional[str]:
    """
    Returns 'BULLISH', 'BEARISH', or 'NEUTRAL'.

    Uses TWO signals combined so a lagging EMA-20 can't block a strong intraday move:
      1. EMA-8 slope over last 3 bars  (fast, reacts within ~8 min)
      2. Net price change over last 10 bars (raw momentum, no lag)

    Rule: both must agree to call BULLISH or BEARISH; otherwise NEUTRAL.
    This prevents EMA-20 from staying "BULLISH" while price is -50pts in 30 min.
    """
    if df is None or len(df) < 15:
        return None
    df = df.copy().sort_values("datetime").reset_index(drop=True)

    ema8 = _ema(df["close"], 8)
    ema8_now  = float(ema8.iloc[-1])
    ema8_3ago = float(ema8.iloc[-4])           # slope over last 3 bars
    ema8_rising  = ema8_now > ema8_3ago
    ema8_falling = ema8_now < ema8_3ago

    # Raw 10-bar momentum: positive = price went up, negative = price went down
    close_now   = float(df["close"].iloc[-1])
    close_10ago = float(df["close"].iloc[-11]) if len(df) >= 11 else float(df["close"].iloc[0])
    momentum = close_now - close_10ago

    mom_bullish = momentum > 0
    mom_bearish = momentum < 0

    if ema8_rising and mom_bullish:
        return "BULLISH"
    if ema8_falling and mom_bearish:
        return "BEARISH"
    return "NEUTRAL"


def _check_5min_smi_direction(target_direction: str) -> bool:
    """
    Fetches 5-minute bars and checks if 5-min SMI agrees with the 1-min signal.
    Returns True if confirmed, False if conflicting (skip trade).
    """
    hist5 = _fetch_futures_history_best_effort(minutes=240, interval_minutes=CONFIRM_INTERVAL_MINUTES)
    if hist5 is None or hist5.empty:
        log.warning("[SPX0DTE] 5-min confirmation chart unavailable — allowing trade without MTF confirm")
        return True  # Fail-open: allow if we can't fetch

    smi5 = compute_smi(hist5).dropna(subset=["smi_fast", "smi_slow"])
    if len(smi5) < 5:
        return True

    fast_curr = float(smi5["smi_fast"].iloc[-1])
    fast_prev = float(smi5["smi_fast"].iloc[-2])
    slow_curr = float(smi5["smi_slow"].iloc[-1])

    rising_5m = fast_curr > fast_prev
    above_slow_5m = fast_curr > slow_curr

    if target_direction == "CALL":
        confirmed = rising_5m or above_slow_5m
    else:  # PUT
        confirmed = (not rising_5m) or (not above_slow_5m)

    log.info(
        "[SPX0DTE] 5-min MTF confirm | dir=%s fast=%.2f slow=%.2f rising=%s above_slow=%s confirmed=%s",
        target_direction, fast_curr, slow_curr, rising_5m, above_slow_5m, confirmed
    )
    return confirmed


# ─── v4: /ES MOMENTUM ANALYSIS ────────────────────────────────────────────────
# These functions analyze /ES futures directly to determine direction and
# expected move magnitude. They REPLACE SMI as the primary signal source.

def _compute_es_velocity(df: pd.DataFrame, bars: int = VELOCITY_WINDOW_BARS) -> Optional[float]:
    """
    Average absolute /ES price change per bar over the last N bars.
    This is our estimate of "how fast is /ES moving right now" in points/min.
    """
    if df is None or len(df) < bars + 1:
        return None
    closes = df["close"].iloc[-(bars + 1):].values
    diffs = np.abs(np.diff(closes))
    if len(diffs) == 0:
        return None
    velocity = float(np.mean(diffs))
    log.debug("[v4] /ES velocity (last %d bars): %.3f pts/min", bars, velocity)
    return velocity


def _compute_range_expansion(df: pd.DataFrame) -> Optional[float]:
    """
    Ratio of current bar range to mean of prior N bars.
    > 1.3 = expanding (continuation likely)
    < 0.7 = contracting (chop, no follow-through)
    """
    if df is None or len(df) < RANGE_EXPANSION_LOOKBACK + 1:
        return None
    ranges = (df["high"] - df["low"]).iloc[-(RANGE_EXPANSION_LOOKBACK + 1):]
    current = float(ranges.iloc[-1])
    prior_mean = float(ranges.iloc[:-1].mean())
    if prior_mean <= 0:
        return None
    ratio = current / prior_mean
    log.debug("[v4] /ES range expansion: current=%.2f prior_avg=%.2f ratio=%.2f",
              current, prior_mean, ratio)
    return ratio


def _compute_es_vwap(df: pd.DataFrame) -> Optional[pd.Series]:
    """
    Session-anchored VWAP from today's /ES bars.
    Returns full series so caller can check slope.
    """
    if df is None or df.empty or "volume" not in df.columns:
        return None
    today = _now_et().date()
    out = df.copy()
    out["date_et"] = out["datetime"].dt.date
    sess = out[out["date_et"] == today].copy()
    if sess.empty or sess["volume"].sum() <= 0:
        sess = out.tail(60).copy()
        if sess["volume"].sum() <= 0:
            return None
    tp = (sess["high"] + sess["low"] + sess["close"]) / 3.0
    vwap = (tp * sess["volume"]).cumsum() / sess["volume"].cumsum()
    return vwap


def _detect_5m_structure(df_1m: pd.DataFrame) -> str:
    """
    Resample 1m /ES to 5m and check structural pattern.
    Returns: 'HH_HL' (uptrend), 'LH_LL' (downtrend), 'MIXED' (no clear structure).
    """
    if df_1m is None or len(df_1m) < STRUCTURAL_BARS_5M * 5 + 5:
        return "MIXED"
    try:
        d = df_1m.copy().set_index("datetime")
        bars5 = d[["high", "low", "close"]].resample("5min").agg(
            {"high": "max", "low": "min", "close": "last"}
        ).dropna().tail(STRUCTURAL_BARS_5M)
        if len(bars5) < 4:
            return "MIXED"
        highs = bars5["high"].values
        lows = bars5["low"].values
        # Two consecutive HH and HL = uptrend; LH and LL = downtrend
        recent_higher_highs = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
        recent_higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
        recent_lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
        recent_lower_lows = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i-1])
        n = len(highs) - 1
        if recent_higher_highs >= n * 0.6 and recent_higher_lows >= n * 0.5:
            return "HH_HL"
        if recent_lower_highs >= n * 0.6 and recent_lower_lows >= n * 0.5:
            return "LH_LL"
        return "MIXED"
    except Exception as e:
        log.warning("[v4] _detect_5m_structure failed: %s", e)
        return "MIXED"


def detect_es_signal() -> Tuple[str, Dict[str, Any]]:
    """
    v4 PRIMARY SIGNAL: read /ES directly to determine direction + edge state.

    Returns:
      signal: 'CALL' | 'PUT' | 'NONE'
      data:   diagnostic dict with edge_state, velocity, expansion, vwap_pos, etc.
    """
    data: Dict[str, Any] = {
        "signal": "NONE",
        "reason": "",
        "method": "ES_MOMENTUM_v4",
        "edge_state": "UNKNOWN",
        "velocity": None,
        "expansion_ratio": None,
        "price_vs_vwap": None,
        "vwap_slope": None,
        "structure_5m": None,
        "expected_move_5min": None,
        "fast_prev": None, "fast_curr": None,  # SMI tiebreaker (compat)
        "interval_minutes": CHART_INTERVAL_MINUTES,
        "symbol_used": ES_FUTURES,
        "last_bar_time": None,
    }

    log.info("[v4] Reading /ES for direction + edge state")

    hist = _fetch_futures_history_best_effort(minutes=240, interval_minutes=CHART_INTERVAL_MINUTES)
    if hist is None or hist.empty:
        data["reason"] = "/ES history unavailable"
        return "NONE", data

    if len(hist) < 30:
        data["reason"] = f"Insufficient /ES bars: {len(hist)}"
        return "NONE", data

    data["last_bar_time"] = str(hist["datetime"].iloc[-1])

    # ── Velocity ──────────────────────────────────────────────────────────────
    velocity = _compute_es_velocity(hist)
    data["velocity"] = round(velocity, 3) if velocity else None
    if velocity is None:
        data["reason"] = "Could not compute /ES velocity"
        return "NONE", data
    if velocity < MIN_ES_VELOCITY:
        data["edge_state"] = "CHOP"
        data["reason"] = f"/ES velocity too low: {velocity:.2f} pts/min < {MIN_ES_VELOCITY}"
        log.info("[v4] 🚫 %s", data["reason"])
        return "NONE", data

    # ── Range expansion ───────────────────────────────────────────────────────
    expansion = _compute_range_expansion(hist)
    data["expansion_ratio"] = round(expansion, 2) if expansion else None

    # ── VWAP relationship + slope ─────────────────────────────────────────────
    vwap_series = _compute_es_vwap(hist)
    if vwap_series is None or len(vwap_series) < VWAP_SLOPE_BARS + 1:
        data["reason"] = "Could not compute /ES VWAP"
        return "NONE", data

    price_now = float(hist["close"].iloc[-1])
    vwap_now = float(vwap_series.iloc[-1])
    vwap_prior = float(vwap_series.iloc[-(VWAP_SLOPE_BARS + 1)])
    vwap_slope = (vwap_now - vwap_prior) / VWAP_SLOPE_BARS

    if price_now > vwap_now:
        price_vs_vwap = "ABOVE"
    elif price_now < vwap_now:
        price_vs_vwap = "BELOW"
    else:
        price_vs_vwap = "AT"

    data["price_vs_vwap"] = price_vs_vwap
    data["vwap_slope"] = round(vwap_slope, 3)

    # ── 5-min structural ──────────────────────────────────────────────────────
    structure = _detect_5m_structure(hist)
    data["structure_5m"] = structure

    # ── Determine direction ───────────────────────────────────────────────────
    bullish_signals = 0
    bearish_signals = 0
    if price_vs_vwap == "ABOVE":
        bullish_signals += 1
    elif price_vs_vwap == "BELOW":
        bearish_signals += 1
    if vwap_slope >= VWAP_SLOPE_MIN:
        bullish_signals += 1
    elif vwap_slope <= -VWAP_SLOPE_MIN:
        bearish_signals += 1
    if structure == "HH_HL":
        bullish_signals += 1
    elif structure == "LH_LL":
        bearish_signals += 1

    if bullish_signals >= 2 and bearish_signals == 0:
        direction = "CALL"
        signal_strength = bullish_signals
    elif bearish_signals >= 2 and bullish_signals == 0:
        direction = "PUT"
        signal_strength = bearish_signals
    else:
        direction = None
        signal_strength = max(bullish_signals, bearish_signals)

    # ── Determine edge state ──────────────────────────────────────────────────
    expanding = expansion is not None and expansion >= RANGE_EXPANSION_THRESHOLD

    if direction is not None and signal_strength == 3 and expanding:
        edge_state = "STRONG_TREND"
    elif direction is not None and signal_strength >= 2:
        edge_state = "WEAK_TREND"
    else:
        edge_state = "CHOP"

    data["edge_state"] = edge_state

    # ── Tiebreaker: SMI for borderline calls ──────────────────────────────────
    # SMI is computed but only consulted when /ES signals are ambiguous.
    if direction is None or edge_state == "WEAK_TREND":
        try:
            smi_df = compute_smi(hist).dropna(subset=["smi_fast", "smi_slow"]).reset_index(drop=True)
            if len(smi_df) >= 5:
                f_curr = float(smi_df["smi_fast"].iloc[-1])
                f_prev = float(smi_df["smi_fast"].iloc[-2])
                data["fast_prev"] = round(f_prev, 2)
                data["fast_curr"] = round(f_curr, 2)
                if direction is None:
                    if f_curr > f_prev and f_curr > -20:
                        direction = "CALL"
                    elif f_curr < f_prev and f_curr < 20:
                        direction = "PUT"
        except Exception:
            pass

    # ── Expected /ES move estimate ────────────────────────────────────────────
    # Velocity is pts/min on /ES; over 5 min in trend = velocity * 5 * direction_factor
    direction_factor = 1.5 if edge_state == "STRONG_TREND" else 0.8
    expected_move_5min = velocity * 5 * direction_factor
    data["expected_move_5min"] = round(expected_move_5min, 2)

    # ── Apply edge-state gate ─────────────────────────────────────────────────
    if direction is None:
        data["reason"] = (
            f"No direction | bull_signals={bullish_signals} bear_signals={bearish_signals} "
            f"vwap={price_vs_vwap} slope={vwap_slope:.3f} struct={structure}"
        )
        log.info("[v4] ❌ %s", data["reason"])
        return "NONE", data

    if edge_state not in ALLOWED_EDGE_STATES:
        data["reason"] = (
            f"{direction} signal blocked: edge_state={edge_state} "
            f"not in {ALLOWED_EDGE_STATES}"
        )
        log.info("[v4] 🚫 %s", data["reason"])
        return "NONE", data

    data["signal"] = direction
    data["reason"] = (
        f"{direction} | edge={edge_state} | vel={velocity:.2f}pts/min | "
        f"exp={expansion:.2f} | vwap={price_vs_vwap}/slope{vwap_slope:+.3f} | "
        f"struct={structure} | expected_5m_move={expected_move_5min:.1f}pts"
    )
    log.info("[v4] ✅ %s signal: %s", direction, data["reason"])
    return direction, data


# ─── v4: DECAY-AWARE EDGE EVALUATION ──────────────────────────────────────────

def _theta_minutes_remaining_factor() -> float:
    """
    Returns multiplier for theta acceleration based on time-of-day.
    Theta on 0DTE is non-linear — much steeper after 13:30.
    """
    now = _now_et()
    h = now.hour
    if h < 11:
        return 1.0
    elif h < 12:
        return 1.3
    elif h < 13:
        return 1.6
    elif h < 14:
        return 2.2
    elif h < 15:
        return 3.0
    else:
        return 4.5


def evaluate_decay_edge(
    opt: Dict[str, Any],
    underlying: float,
    expected_move_pts: float,
    expected_hold_min: int = 10,
) -> Tuple[float, Dict[str, Any]]:
    """
    Calculate decay-adjusted edge for a candidate option.

    Returns:
      edge_score: positive = trade has edge, negative = decay > expected gain
      diagnostics dict
    """
    diag: Dict[str, Any] = {}
    premium = float(opt["premium"] or 0.0)
    delta = abs(float(opt["delta"] or 0.0))
    raw_theta = abs(float(opt.get("theta", 0.0) or 0.0))
    spread = float(opt.get("spread", 999) or 999)

    if premium <= 0 or delta <= 0:
        diag["reject"] = "invalid premium/delta"
        return -1000.0, diag

    # If theta missing from chain, estimate it conservatively
    # For 0DTE ATM options, theta is typically 5-20% of premium per hour
    if raw_theta == 0:
        # Worst-case estimate: assume premium decays 20% per hour
        estimated_hourly_theta = premium * 0.20
        raw_theta = estimated_hourly_theta / 24  # theta is usually per-day in chains
        diag["theta_estimated"] = True
    else:
        diag["theta_estimated"] = False

    # Theta on options is daily; convert to per-minute, apply time-of-day factor
    tod_factor = _theta_minutes_remaining_factor()
    theta_per_min = (raw_theta / 390.0) * tod_factor

    decay_cost_dollars = theta_per_min * expected_hold_min * 100  # in dollars per contract
    decay_cost_pts = decay_cost_dollars / 100.0  # in option price points

    # Estimated option price gain from /ES move
    # Note: /ES point ≈ SPX point at this scale
    estimated_gain_pts = delta * expected_move_pts

    # Slippage allowance (roundtrip)
    slippage_pts = max(0.10, spread * 0.5)

    # Net expected profit in option points
    net_profit_pts = estimated_gain_pts - decay_cost_pts - slippage_pts

    # Edge score = net profit divided by potential downside
    # Downside ~= decay_cost + slippage if /ES doesn't move
    downside_pts = decay_cost_pts + slippage_pts
    edge_ratio = estimated_gain_pts / downside_pts if downside_pts > 0 else 0

    diag.update({
        "premium": round(premium, 2),
        "delta": round(delta, 3),
        "raw_theta": round(raw_theta, 4),
        "theta_per_min": round(theta_per_min, 5),
        "tod_factor": round(tod_factor, 2),
        "decay_cost_pts": round(decay_cost_pts, 3),
        "estimated_gain_pts": round(estimated_gain_pts, 3),
        "slippage_pts": round(slippage_pts, 3),
        "net_profit_pts": round(net_profit_pts, 3),
        "edge_ratio": round(edge_ratio, 2),
        "expected_move_pts": expected_move_pts,
        "expected_hold_min": expected_hold_min,
    })

    # Theta percentage of premium check
    theta_pct = decay_cost_dollars / (premium * 100) if premium > 0 else 1.0
    diag["theta_pct_per_10min"] = round(theta_pct, 3)

    if theta_pct > MAX_THETA_PCT_PER_10MIN:
        diag["reject"] = f"theta_pct {theta_pct:.2%} > max {MAX_THETA_PCT_PER_10MIN:.2%}"
        return -100.0, diag

    if edge_ratio < EDGE_RATIO_MIN:
        diag["reject"] = f"edge_ratio {edge_ratio:.2f} < min {EDGE_RATIO_MIN}"
        return edge_ratio - EDGE_RATIO_MIN, diag

    # Final edge score: scale net_profit by edge_ratio
    edge_score = net_profit_pts * edge_ratio
    diag["edge_score"] = round(edge_score, 3)
    return edge_score, diag


def _delta_floor_for_now() -> float:
    """Time-of-day-adjusted minimum delta requirement."""
    h = _now_et().hour
    return DELTA_FLOOR_BY_HOUR.get(h, MIN_DELTA)


# ─── Signal Detection ─────────────────────────────────────────────────────────

def detect_smi_reversal_signal() -> Tuple[str, Dict[str, Any]]:
    data: Dict[str, Any] = {
        "signal": "NONE",
        "reason": "",
        "method": f"SMI_{CHART_INTERVAL_MINUTES}M_CROSS_REVERSAL_v2",
        "fast_prev": None,
        "fast_curr": None,
        "slow_prev": None,
        "slow_curr": None,
        "recent_min": None,
        "recent_max": None,
        "rising": None,
        "falling": None,
        "crossed_up_oversold": None,
        "crossed_down_overbought": None,
        "bull_cross": None,
        "bear_cross": None,
        "bull_signal": None,
        "bear_signal": None,
        "ema20_trend": None,
        "mtf_confirmed": None,
        "momentum_ok": None,
        "strict_level_cross": STRICT_LEVEL_CROSS,
        "lookback": REVERSAL_LOOKBACK_CANDLES,
        "overbought": SMI_OVER_BOUGHT,
        "oversold": SMI_OVER_SOLD,
        "interval_minutes": CHART_INTERVAL_MINUTES,
        "symbol_used": ES_FUTURES,
        "last_bar_time": None,
    }

    log.info("[SPX0DTE] Detecting SMI reversal signal using %dmin bars from %s",
             CHART_INTERVAL_MINUTES, ES_FUTURES)

    hist = _fetch_futures_history_best_effort(minutes=240, interval_minutes=CHART_INTERVAL_MINUTES)
    if hist is None or hist.empty:
        data["reason"] = f"No {ES_FUTURES} history available"
        return "NONE", data

    smi_df = compute_smi(hist).dropna(subset=["smi_fast", "smi_slow"]).reset_index(drop=True)
    if len(smi_df) < max(60, REVERSAL_LOOKBACK_CANDLES + 5):
        data["reason"] = f"Not enough candles after SMI: {len(smi_df)}"
        return "NONE", data

    # ── Core SMI values ──
    prev_fast = float(smi_df["smi_fast"].iloc[-2])
    curr_fast = float(smi_df["smi_fast"].iloc[-1])
    prev_slow = float(smi_df["smi_slow"].iloc[-2])
    curr_slow = float(smi_df["smi_slow"].iloc[-1])
    prior_fast = float(smi_df["smi_fast"].iloc[-3])  # 3 bars ago for momentum

    data.update({
        "fast_prev": round(prev_fast, 4),
        "fast_curr": round(curr_fast, 4),
        "slow_prev": round(prev_slow, 4),
        "slow_curr": round(curr_slow, 4),
        "last_bar_time": str(smi_df["datetime"].iloc[-1]),
    })

    recent_fast = smi_df["smi_fast"].iloc[-REVERSAL_LOOKBACK_CANDLES:]
    recent_min = float(recent_fast.min())
    recent_max = float(recent_fast.max())
    data["recent_min"] = round(recent_min, 2)
    data["recent_max"] = round(recent_max, 2)

    rising = curr_fast > prev_fast
    falling = curr_fast < prev_fast
    data["rising"] = rising
    data["falling"] = falling

    # ── Momentum gate: fast line must have moved >= threshold over last 2 bars ──
    momentum_bull = (curr_fast - prior_fast) >= SMI_MOMENTUM_MIN_MOVE
    momentum_bear = (prior_fast - curr_fast) >= SMI_MOMENTUM_MIN_MOVE
    data["momentum_ok"] = momentum_bull or momentum_bear

    # Standard level-cross: fast crossed the threshold on this exact bar
    crossed_up_oversold      = (prev_fast <= SMI_OVER_SOLD)  and (curr_fast > SMI_OVER_SOLD)
    crossed_down_overbought  = (prev_fast >= SMI_OVER_BOUGHT) and (curr_fast < SMI_OVER_BOUGHT)

    # Extended level-cross: fast was in overbought/oversold territory within lookback
    # even if the actual threshold cross happened a few bars ago.
    recently_was_oversold   = recent_min <= SMI_OVER_SOLD
    recently_was_overbought = recent_max >= SMI_OVER_BOUGHT

    # Promote to "crossed" if the condition was met recently and direction agrees
    if not crossed_up_oversold and recently_was_oversold and rising:
        crossed_up_oversold = True
        log.info("[SPX0DTE] crossed_up_oversold promoted via recent_min=%.2f", recent_min)
    if not crossed_down_overbought and recently_was_overbought and falling:
        crossed_down_overbought = True
        log.info("[SPX0DTE] crossed_down_overbought promoted via recent_max=%.2f", recent_max)
    data["crossed_up_oversold"] = crossed_up_oversold
    data["crossed_down_overbought"] = crossed_down_overbought

    bull_cross = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
    bear_cross = (prev_fast >= prev_slow) and (curr_fast < curr_slow)

    # ── "Already crossed / sustained trend" detection ────────────────────────
    # On strong trending days the cross happened earlier and the bot
    # never sees a fresh cross again.  If fast has been below slow for
    # the entire lookback window AND is still falling, treat that as an
    # implicit bear cross.  Same logic inverted for bull.
    lookback_fast = smi_df["smi_fast"].iloc[-REVERSAL_LOOKBACK_CANDLES:]
    lookback_slow = smi_df["smi_slow"].iloc[-REVERSAL_LOOKBACK_CANDLES:]
    fast_sustained_below = bool((lookback_fast < lookback_slow).all())
    fast_sustained_above = bool((lookback_fast > lookback_slow).all())

    if not bear_cross and fast_sustained_below and falling:
        bear_cross = True
        log.info("[SPX0DTE] bear_cross promoted: fast sustained below slow for %d bars",
                 REVERSAL_LOOKBACK_CANDLES)

    if not bull_cross and fast_sustained_above and rising:
        bull_cross = True
        log.info("[SPX0DTE] bull_cross promoted: fast sustained above slow for %d bars",
                 REVERSAL_LOOKBACK_CANDLES)
    data["bull_cross"] = bull_cross
    data["bear_cross"] = bear_cross

    # ── EMA-20 trend filter ──
    ema_trend = _compute_ema20_trend(hist)
    data["ema20_trend"] = ema_trend

    # ── Build base signal ──
    if STRICT_LEVEL_CROSS:
        bull_signal = recent_min <= SMI_OVER_SOLD and rising and bull_cross and crossed_up_oversold
        bear_signal = recent_max >= SMI_OVER_BOUGHT and falling and bear_cross and crossed_down_overbought
    else:
        bull_signal = recent_min <= SMI_OVER_SOLD and rising and bull_cross
        bear_signal = recent_max >= SMI_OVER_BOUGHT and falling and bear_cross

    data["bull_signal"] = bull_signal
    data["bear_signal"] = bear_signal

    # ── Apply trend filter ──
    # Block signals that fight the EMA-20 trend; neutral trend is allowed
    if bull_signal and ema_trend == "BEARISH":
        data["reason"] = (
            f"CALL signal blocked: EMA-20 trend is BEARISH "
            f"(fast={curr_fast:.2f}, trend={ema_trend})"
        )
        log.info("[SPX0DTE] 🚫 %s", data["reason"])
        return "NONE", data

    if bear_signal and ema_trend == "BULLISH":
        data["reason"] = (
            f"PUT signal blocked: EMA-20 trend is BULLISH "
            f"(fast={curr_fast:.2f}, trend={ema_trend})"
        )
        log.info("[SPX0DTE] 🚫 %s", data["reason"])
        return "NONE", data

    # ── Apply momentum gate ──
    if bull_signal and not momentum_bull:
        data["reason"] = (
            f"CALL signal blocked: insufficient momentum "
            f"(fast moved {curr_fast - prior_fast:.2f}pts, need >={SMI_MOMENTUM_MIN_MOVE})"
        )
        log.info("[SPX0DTE] 🚫 %s", data["reason"])
        return "NONE", data

    if bear_signal and not momentum_bear:
        data["reason"] = (
            f"PUT signal blocked: insufficient momentum "
            f"(fast moved {prior_fast - curr_fast:.2f}pts, need >={SMI_MOMENTUM_MIN_MOVE})"
        )
        log.info("[SPX0DTE] 🚫 %s", data["reason"])
        return "NONE", data

    log.info(
        "[SPX0DTE] SMI %dmin | fast_prev=%.2f fast_curr=%.2f | "
        "rising=%s falling=%s | recent_min=%.2f recent_max=%.2f | "
        "bull_cross=%s bear_cross=%s | crossOS=%s crossOB=%s | "
        "bull=%s bear=%s | trend=%s | momentum_bull=%s bear=%s",
        CHART_INTERVAL_MINUTES, prev_fast, curr_fast,
        rising, falling, recent_min, recent_max,
        bull_cross, bear_cross, crossed_up_oversold, crossed_down_overbought,
        bull_signal, bear_signal, ema_trend, momentum_bull, momentum_bear,
    )

    if bull_signal:
        # ── Multi-timeframe confirmation ──
        mtf_ok = _check_5min_smi_direction("CALL")
        data["mtf_confirmed"] = mtf_ok
        if not mtf_ok:
            data["reason"] = "CALL signal blocked: 5-min SMI disagrees (PUT bias on higher timeframe)"
            log.info("[SPX0DTE] 🚫 %s", data["reason"])
            return "NONE", data

        data["signal"] = "CALL"
        data["reason"] = (
            f"Bullish reversal: recent_min={recent_min:.2f}<={SMI_OVER_SOLD}; "
            f"fast {prev_fast:.2f}->{curr_fast:.2f}; bull_cross={bull_cross}; "
            f"crossOS={crossed_up_oversold}; trend={ema_trend}; mtf=OK"
        )
        log.info("[SPX0DTE] ✅ CALL signal: %s", data["reason"])
        return "CALL", data

    if bear_signal:
        mtf_ok = _check_5min_smi_direction("PUT")
        data["mtf_confirmed"] = mtf_ok
        if not mtf_ok:
            data["reason"] = "PUT signal blocked: 5-min SMI disagrees (CALL bias on higher timeframe)"
            log.info("[SPX0DTE] 🚫 %s", data["reason"])
            return "NONE", data

        data["signal"] = "PUT"
        data["reason"] = (
            f"Bearish reversal: recent_max={recent_max:.2f}>={SMI_OVER_BOUGHT}; "
            f"fast {prev_fast:.2f}->{curr_fast:.2f}; bear_cross={bear_cross}; "
            f"crossOB={crossed_down_overbought}; trend={ema_trend}; mtf=OK"
        )
        log.info("[SPX0DTE] ✅ PUT signal: %s", data["reason"])
        return "PUT", data

    data["reason"] = (
        f"No reversal trigger | rising={rising} falling={falling} "
        f"recent_min={recent_min:.2f} recent_max={recent_max:.2f} "
        f"bull_cross={bull_cross} bear_cross={bear_cross} trend={ema_trend}"
    )
    log.info("[SPX0DTE] ❌ NO signal: %s", data["reason"])
    return "NONE", data


# ─── Risk Metrics ─────────────────────────────────────────────────────────────

def get_risk_metrics(user_id: Optional[int] = None) -> Dict[str, Any]:
    metrics = {
        "daily_pnl": 0.0,
        "consecutive_losses": 0,
        "can_trade": True,
        "trades_today": 0,
        "reason": "",
        "last_trade_closed_at": None,
        "minutes_since_last_loss": None,
    }

    try:
        db = SessionLocal()
        today = _today_et()
        day_start = datetime.combine(today, datetime.min.time())

        trades = db.query(PaperSPXTradeHistory).filter(
            PaperSPXTradeHistory.user_id == (user_id or 0),
            PaperSPXTradeHistory.closed_at >= day_start,
        ).order_by(PaperSPXTradeHistory.closed_at).all()

        for t in trades:
            metrics["daily_pnl"] += float(t.pnl_usd or 0)
        metrics["trades_today"] = len(trades)

        if trades:
            for t in reversed(trades):
                if float(t.pnl_usd or 0) < 0:
                    metrics["consecutive_losses"] += 1
                else:
                    break

            last_trade = trades[-1]
            metrics["last_trade_closed_at"] = str(last_trade.closed_at)

            # Cooldown after loss
            last_pnl = float(last_trade.pnl_usd or 0)
            if last_pnl < 0 and last_trade.closed_at:
                closed_utc = last_trade.closed_at.replace(tzinfo=UTC) \
                    if last_trade.closed_at.tzinfo is None else last_trade.closed_at
                mins_since = (datetime.now(UTC) - closed_utc).total_seconds() / 60.0
                metrics["minutes_since_last_loss"] = round(mins_since, 1)
                if mins_since < COOLDOWN_AFTER_LOSS_MINUTES:
                    metrics["can_trade"] = False
                    metrics["reason"] = (
                        f"Cooling down after loss: {mins_since:.1f}min elapsed "
                        f"(need {COOLDOWN_AFTER_LOSS_MINUTES}min)"
                    )
                    log.info("[SPX0DTE] %s", metrics["reason"])
                    db.close()
                    return metrics

        if metrics["daily_pnl"] <= -MAX_DAILY_LOSS:
            metrics["can_trade"] = False
            metrics["reason"] = f"Daily loss limit: ${metrics['daily_pnl']:.2f}"
        elif metrics["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            metrics["can_trade"] = False
            metrics["reason"] = f"{metrics['consecutive_losses']} consecutive losses — stop for the day"
        elif metrics["trades_today"] >= MAX_TRADES_PER_DAY:
            metrics["can_trade"] = False
            metrics["reason"] = f"Max daily trades reached: {metrics['trades_today']}"
        else:
            metrics["reason"] = "OK to trade"

        log.info(
            "[SPX0DTE] Risk | daily_pnl=$%.2f trades=%d consec_losses=%d can_trade=%s",
            metrics["daily_pnl"], metrics["trades_today"],
            metrics["consecutive_losses"], metrics["can_trade"]
        )
        db.close()
    except Exception as e:
        log.warning("[SPX0DTE] Risk metrics failed: %s", e)

    return metrics


def _check_signal_cooldown(user_id: Optional[int] = None) -> bool:
    """
    Returns True if it's safe to place a new signal (cooldown elapsed).
    Prevents rapid-fire duplicate signals (fixed the 12:17:02 double-fire bug).
    """
    try:
        db = SessionLocal()
        today = _today_et()
        day_start = datetime.combine(today, datetime.min.time())
        cutoff = datetime.utcnow() - timedelta(minutes=COOLDOWN_AFTER_SIGNAL_MINUTES)

        recent_picks = db.query(PaperSPXPick).filter(
            PaperSPXPick.user_id == (user_id or 0),
            PaperSPXPick.run_date == today,
            PaperSPXPick.created_at >= cutoff,
        ).count()
        db.close()

        if recent_picks > 0:
            log.info(
                "[SPX0DTE] Signal cooldown active: %d picks in last %dmin",
                recent_picks, COOLDOWN_AFTER_SIGNAL_MINUTES
            )
            return False
        return True
    except Exception as e:
        log.warning("[SPX0DTE] Signal cooldown check failed: %s", e)
        return True  # fail-open


# ─── Option Selection ─────────────────────────────────────────────────────────

def _extract_option_fields(row: pd.Series) -> Dict[str, Any]:
    cols = {c.lower(): c for c in row.index}

    def g(*names, default=None):
        for name in names:
            real = cols.get(name.lower())
            if real is not None:
                return row.get(real, default)
        return default

    bid = _as_float(g("bid"), 0.0)
    ask = _as_float(g("ask"), 0.0)
    mark = _as_float(g("mark"), 0.0)
    last = _as_float(g("last"), 0.0)
    delta = _as_float(g("delta"), 0.0)
    theta = _as_float(g("theta"), 0.0)
    gamma = _as_float(g("gamma"), 0.0)
    vega = _as_float(g("vega"), 0.0)
    iv = _as_float(g("volatility", "impliedvolatility"), 0.0)

    premium = 0.0
    if ask > 0:
        premium = ask
    elif mark > 0:
        premium = mark
    elif last > 0:
        premium = last
    elif bid > 0 and ask > 0:
        premium = (bid + ask) / 2.0

    spread = (ask - bid) if bid > 0 and ask > 0 else 999.0

    symbol = str(g("symbol", "contractsymbol", "option_symbol", default="") or "").strip()
    strike = _as_float(g("strike", "strikeprice"), 0.0)
    volume = _as_float(g("volume", "totalvolume"), 0.0)
    oi = _as_float(g("open_interest", "openinterest"), 0.0)

    raw_pc = str(g("putcall", "put_call", "type", "contracttype", "option_type", "callput", default="") or "").strip().upper()
    put_call = ""
    if raw_pc in ("PUT", "P"):
        put_call = "PUT"
    elif raw_pc in ("CALL", "C"):
        put_call = "CALL"
    else:
        sym_u = symbol.upper().replace(" ", "")
        if "P" in sym_u[-15:]:
            put_call = "PUT"
        elif "C" in sym_u[-15:]:
            put_call = "CALL"

    return {
        "symbol": symbol,
        "put_call": put_call,
        "strike": strike,
        "bid": round(bid, 2),
        "ask": round(ask, 2),
        "mark": round(mark, 2),
        "last": round(last, 2),
        "premium": round(premium, 2),
        "spread": round(spread, 2) if spread != 999.0 else 999.0,
        "volume": volume,
        "open_interest": oi,
        "delta": round(delta, 4),
        "theta": round(theta, 4),    # NEW v4
        "gamma": round(gamma, 4),    # NEW v4
        "vega": round(vega, 4),      # NEW v4
        "iv": round(iv, 4),          # NEW v4
    }


def _filter_option_side(df: pd.DataFrame, put_call: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    side = put_call.upper()
    rows = [row for _, row in df.iterrows() if _extract_option_fields(row)["put_call"] == side]
    if not rows:
        log.warning("[SPX0DTE] No %s rows after side filter", side)
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)


def _score_candidate(opt: Dict[str, Any], underlying: float, put_call: str,
                     expected_move_pts: float = 5.0) -> float:
    """
    v4 SCORING: decay-aware option scoring.

    The score is dominated by edge_score from evaluate_decay_edge — the
    expected dollar profit after theta and slippage are subtracted.
    Liquidity and tightness are minor tiebreakers.

    expected_move_pts is the /ES move we expect over the hold period;
    larger expected moves favor higher-delta strikes; smaller moves
    favor cheaper OTM strikes that need only a small relative move.
    """
    spread = float(opt["spread"] or 999.0)
    volume = float(opt["volume"] or 0.0)
    oi = float(opt["open_interest"] or 0.0)

    # Compute decay-adjusted edge — this is THE primary metric
    edge_score, diag = evaluate_decay_edge(
        opt=opt,
        underlying=underlying,
        expected_move_pts=expected_move_pts,
        expected_hold_min=10,
    )
    opt["_decay_diag"] = diag
    opt["_edge_score"] = edge_score

    # Edge_score is in option points; multiply by 100 to convert to dollars per contract
    score = edge_score * 100.0

    # Tiebreakers (small adjustments)
    # Tighter spread = better scalp execution
    if spread <= 0.30:
        score += 5.0
    elif spread <= 0.60:
        score += 2.0
    else:
        score -= spread * 5.0

    # Liquidity (avoid stale-quote contracts)
    score += min(volume, 200) * 0.02
    score += min(oi, 2000) * 0.002

    return round(score, 4)


def select_best_option(
    put_call: str,
    run_date: date,
    expected_move_pts: float = 5.0,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    v4 SELECTION: applies time-of-day delta floor and decay edge filter.
    """
    meta: Dict[str, Any] = {
        "put_call": put_call,
        "reason": "",
        "underlying": None,
        "used_root": None,
        "candidate_count": 0,
        "expected_move_pts": expected_move_pts,
        "tod_delta_floor": _delta_floor_for_now(),
    }

    df, underlying_px, used_root = _fetch_full_option_chain_spx(run_date)
    if df is None or df.empty:
        meta["reason"] = "Option chain unavailable"
        return None, meta
    if not underlying_px:
        meta["reason"] = "Underlying price unavailable"
        return None, meta

    underlying = float(underlying_px)
    meta["underlying"] = round(underlying, 2)
    meta["used_root"] = used_root

    side_df = _filter_option_side(df, put_call)
    if side_df.empty:
        meta["reason"] = f"No {put_call} contracts in chain"
        return None, meta

    # Time-of-day adjusted delta floor
    delta_floor = _delta_floor_for_now()

    rejects = {
        "missing_symbol": 0,
        "premium_range": 0,
        "bid_too_low": 0,
        "spread_wide": 0,
        "delta_range": 0,
        "delta_below_tod_floor": 0,
        "volume": 0,
        "no_edge": 0,
        "high_theta_pct": 0,
    }

    candidates = []
    rejected_with_diag = []

    for _, row in side_df.iterrows():
        opt = _extract_option_fields(row)

        if not opt["symbol"]:
            rejects["missing_symbol"] += 1
            continue

        if opt["premium"] < MIN_PREMIUM or opt["premium"] > MAX_PREMIUM:
            rejects["premium_range"] += 1
            continue

        if opt["bid"] < MIN_BID:
            rejects["bid_too_low"] += 1
            continue

        if opt["spread"] > MAX_SPREAD:
            rejects["spread_wide"] += 1
            continue

        delta_abs = abs(opt["delta"])
        if delta_abs > 0 and (delta_abs < MIN_DELTA or delta_abs > MAX_DELTA):
            rejects["delta_range"] += 1
            continue

        # Time-of-day floor: late in day, require deeper delta
        if delta_abs > 0 and delta_abs < delta_floor:
            rejects["delta_below_tod_floor"] += 1
            continue

        if opt["volume"] < MIN_VOLUME:
            rejects["volume"] += 1
            continue

        # Decay-adjusted edge evaluation
        opt["distance_from_underlying"] = round(abs(float(opt["strike"]) - underlying), 2)
        opt["score"] = _score_candidate(opt, underlying, put_call, expected_move_pts)

        diag = opt.get("_decay_diag", {})
        reject_reason = diag.get("reject")
        if reject_reason:
            if "theta_pct" in reject_reason:
                rejects["high_theta_pct"] += 1
            else:
                rejects["no_edge"] += 1
            rejected_with_diag.append((opt["symbol"], reject_reason))
            continue

        candidates.append(opt)

    meta["candidate_count"] = len(candidates)
    meta["rejects"] = rejects

    if not candidates:
        meta["reason"] = (
            f"No {put_call} contracts passed filters | "
            f"rows={len(side_df)} delta_floor={delta_floor:.2f} rejects={rejects}"
        )
        if rejected_with_diag[:3]:
            meta["sample_rejections"] = rejected_with_diag[:3]
        log.info("[SPX0DTE] %s", meta["reason"])
        return None, meta

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]
    diag = best.get("_decay_diag", {})
    meta["reason"] = (
        f"Selected best {put_call} | score={best['score']:.2f} "
        f"edge_ratio={diag.get('edge_ratio', 0)} "
        f"net_profit_pts={diag.get('net_profit_pts', 0)}"
    )
    meta["best_decay_diag"] = diag

    log.info(
        "[v4] ✅ Best option: %s strike=%.2f premium=%.2f delta=%.2f theta=%.4f "
        "spread=%.2f vol=%.0f | edge_ratio=%.2f net=$%.0f score=%.1f",
        best["symbol"], best["strike"], best["premium"], best["delta"],
        best.get("theta", 0), best["spread"], best["volume"],
        diag.get("edge_ratio", 0),
        (diag.get("net_profit_pts", 0) or 0) * 100,
        best["score"]
    )

    return best, meta


# ─── Trade Payload ────────────────────────────────────────────────────────────

def _make_pick_payload(
    run_date: date,
    max_risk: float,
    user_id: Optional[int] = None,
    risk: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """v4: signal from /ES, option selection decay-aware."""
    # ── Block new entries after cutoff ──
    now_t = _now_et().time()
    if now_t >= NEW_ENTRY_CUTOFF_TIME:
        reason = f"past entry cutoff ({NEW_ENTRY_CUTOFF_TIME})"
        log.info("❌ NO TRADE: %s", reason)
        _write_spx0dte_status(user_id, action="NO_TRADE", signal="NONE", reason=reason, risk=risk)
        return None

    # ── PRIMARY SIGNAL: /ES momentum + edge state ──
    signal, es_data = detect_es_signal()
    if signal not in ("CALL", "PUT"):
        reason = es_data.get("reason") or "No qualifying SPX signal"
        log.info("❌ NO TRADE: %s", reason)
        _write_spx0dte_status(
            user_id,
            action="NO_TRADE",
            signal="NONE",
            reason=reason,
            es_data=es_data,
            risk=risk,
        )
        return None

    expected_move = float(es_data.get("expected_move_5min") or 5.0)

    # ── Option selection (decay-aware) ──
    best, meta = select_best_option(
        put_call=signal,
        run_date=run_date,
        expected_move_pts=expected_move,
    )
    if not best:
        reason = meta.get("reason") or "No option contract passed filters"
        log.info("❌ NO TRADE: %s", reason)
        _write_spx0dte_status(
            user_id,
            action="NO_TRADE",
            signal=signal,
            reason=reason,
            es_data=es_data,
            risk=risk,
            extra={"option_meta": meta},
        )
        return None

    entry_price = float(best["premium"] or 0.0)
    if entry_price <= 0:
        reason = f"Invalid entry price: {entry_price:.2f}"
        log.warning("[v4] %s", reason)
        _write_spx0dte_status(user_id, action="NO_TRADE", signal=signal, reason=reason, es_data=es_data, risk=risk)
        return None

    # ── Risk-based stop ──
    stop_distance = RISK_PER_TRADE / 100.0
    initial_stop = round(max(0.10, entry_price - stop_distance), 2)

    # ── Trail and lock-in levels ──
    arm_trigger = round(entry_price + ARM_PROFIT_POINTS, 2)
    take_profit = round(entry_price + TAKE_PROFIT_POINTS, 2)
    lock_in_trigger = round(entry_price + LOCK_IN_TRIGGER_POINTS, 2)
    lock_in_floor = round(entry_price + LOCK_IN_FLOOR_POINTS, 2)

    # ── Confidence scoring ──
    decay_diag = best.get("_decay_diag", {})
    edge_ratio = float(decay_diag.get("edge_ratio") or 0.0)

    confidence = 55.0
    # /ES edge state
    if es_data.get("edge_state") == "STRONG_TREND":
        confidence += 15.0
    elif es_data.get("edge_state") == "WEAK_TREND":
        confidence += 5.0
    # Decay edge ratio
    if edge_ratio >= 3.0:
        confidence += 15.0
    elif edge_ratio >= 2.0:
        confidence += 8.0
    # 5-min structure agreement
    if (signal == "CALL" and es_data.get("structure_5m") == "HH_HL") or \
       (signal == "PUT" and es_data.get("structure_5m") == "LH_LL"):
        confidence += 8.0
    # /ES velocity above threshold
    velocity = float(es_data.get("velocity") or 0.0)
    if velocity >= MIN_ES_VELOCITY * 2:
        confidence += 5.0
    confidence = min(99.0, confidence)

    payload = {
        "underlying_symbol": SPX_CANONICAL,
        "underlying_price": float(meta["underlying"]),
        "chain_root_used": meta.get("used_root"),
        "occ_symbol": best["symbol"],
        "put_call": signal,
        "position_side": "BUY",
        "strike": float(best["strike"]),
        "expiration": run_date,
        "entry_price": round(entry_price, 2),
        "arm_trigger": arm_trigger,
        "stop": initial_stop,
        "take_profit": take_profit,
        "lock_in_trigger": lock_in_trigger,
        "lock_in_floor": lock_in_floor,
        "confidence": round(float(confidence), 2),
        "mode": "debit",
        "strategy": f"ES_DECAY_v4_K5D3",
        "max_risk": float(max_risk or 100.0),
        "market_analysis": {**es_data, "underlying": meta.get("underlying"),
                             "decay_diag": decay_diag},
        "trade_rationale": (
            f"ENTER {signal} v4 | edge={es_data.get('edge_state')} "
            f"vel={velocity:.2f} exp_move={expected_move:.1f}pts | "
            f"strike={best['strike']:.0f} delta={best['delta']:.2f} "
            f"theta={best.get('theta', 0):.4f} edge_ratio={edge_ratio:.2f} | "
            f"stop=${initial_stop:.2f} TP=${take_profit:.2f} arm=${arm_trigger:.2f}"
        ),
        "score": float(best.get("score", 0.0)),
        "option_delta": float(best.get("delta", 0.0)),
        "option_theta": float(best.get("theta", 0.0)),
        "option_spread": float(best.get("spread", 0.0)),
        "edge_ratio": edge_ratio,
        "expected_move_5min": expected_move,
    }

    log.info(
        "[v4] Pick | %s entry=%.2f stop=%.2f TP=%.2f arm=%.2f conf=%.1f "
        "edge_ratio=%.2f exp_move=%.1f",
        signal, entry_price, initial_stop, take_profit, arm_trigger, confidence,
        edge_ratio, expected_move
    )

    _write_spx0dte_status(
        user_id,
        action="TRADE_CANDIDATE",
        signal=signal,
        reason=payload.get("trade_rationale") or es_data.get("reason") or "Trade candidate selected",
        es_data=es_data,
        risk=risk,
        extra={
            "strike": payload.get("strike"),
            "entry_price": payload.get("entry_price"),
            "take_profit": payload.get("take_profit"),
            "stop": payload.get("stop"),
            "confidence": payload.get("confidence"),
            "edge_ratio": payload.get("edge_ratio"),
        },
    )
    return payload


def calculate_position_size(confidence: float, risk_metrics: Dict[str, Any]) -> int:
    """v4: 1 contract until edge_score validation accumulates."""
    if float(confidence or 0.0) >= MIN_CONFIDENCE_FOR_TRADE and bool(risk_metrics.get("can_trade", True)):
        return 1
    return 0


# ─── Main Trade Runner ────────────────────────────────────────────────────────

def run_spx0dte_tick(
    user_id: Optional[int] = None,
    mode: Optional[str] = None,
    max_risk: float = 100.0,
    run_date: Optional[date] = None,
    strategy: str = "es_decay_v4",
    interval_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    user_id = _spx0dte_effective_user_id(user_id)
    _ensure_file_logger(user_id)

    global CHART_INTERVAL_MINUTES
    if interval_minutes is not None:
        CHART_INTERVAL_MINUTES = interval_minutes

    now = _now_et()
    rd = _forced_run_date() or run_date or _today_et()

    log.info("[SPX0DTE] Tick | user=%s now=%s run_date=%s interval=%dmin",
             user_id, now.isoformat(), rd, CHART_INTERVAL_MINUTES)

    if (not TEST_AFTER_HOURS) and (not _is_open_window(now)):
        reason = "MARKET_CLOSED_OR_AVOID_WINDOW"
        _write_spx0dte_status(user_id, action="SKIPPED", signal="NONE", reason=reason)
        return {"ok": True, "skipped": True, "reason": reason, "now_et": now.isoformat()}

    if (not TEST_AFTER_HOURS) and _is_avoid_window(now):
        reason = "AVOID_WINDOW"
        _write_spx0dte_status(user_id, action="SKIPPED", signal="NONE", reason=reason)
        return {"ok": True, "skipped": True, "reason": reason, "now_et": now.isoformat()}

    db = SessionLocal()
    try:
        # ── Guard: already have an open trade ──
        q = db.query(PaperSPXOpenTrade).filter(PaperSPXOpenTrade.status == "OPEN")
        if user_id:
            q = q.filter(PaperSPXOpenTrade.user_id == user_id)
        if q.first():
            _write_spx0dte_status(user_id, action="SKIPPED", signal="NONE", reason="already_open")
            return {"ok": True, "skipped": True, "reason": "already_open"}

        # ── Guard: risk limits ──
        risk = get_risk_metrics(user_id)
        if not risk.get("can_trade", True):
            reason = f"RISK_LIMITS — {risk.get('reason')}"
            log.info("[SPX0DTE] Skipped: %s", reason)
            _write_spx0dte_status(user_id, action="SKIPPED", signal="NONE", reason=reason, risk=risk)
            return {"ok": True, "skipped": True, "reason": "RISK_LIMITS", "metrics": risk}

        # ── Guard: signal cooldown (prevents same-minute double fires) ──
        if not _check_signal_cooldown(user_id):
            _write_spx0dte_status(user_id, action="SKIPPED", signal="NONE", reason="SIGNAL_COOLDOWN", risk=risk)
            return {"ok": True, "skipped": True, "reason": "SIGNAL_COOLDOWN"}

        # ── Generate signal + option ──
        payload = _make_pick_payload(run_date=rd, max_risk=max_risk, user_id=user_id, risk=risk)
        if not payload:
            return {"ok": False, "reason": "no_signal_or_no_contract"}

        confidence = float(payload.get("confidence", 0) or 0.0)
        qty = calculate_position_size(confidence, risk)
        if qty <= 0:
            reason = f"LOW_CONFIDENCE={confidence:.2f}"
            log.info("[SPX0DTE] Skipped: %s", reason)
            _write_spx0dte_status(user_id, action="SKIPPED", signal=payload.get("put_call", "NONE"), reason=reason, risk=risk, extra={"confidence": confidence})
            return {"ok": True, "skipped": True, "reason": "LOW_CONFIDENCE", "confidence": confidence}

        # ── Save pick ──
        pick = PaperSPXPick(
            user_id=int(user_id) if user_id else 0,
            bot_name=_safe_varchar(f"SPX_0DTE_ES_DECAY_{CHART_INTERVAL_MINUTES}M_v4", 32),
            run_date=rd,
            mode=_safe_varchar("debit", 32),
            strategy=_safe_varchar(payload.get("strategy"), 32),
            max_risk=float(payload.get("max_risk") or 100.0),
            underlying_symbol=_safe_varchar(SPX_CANONICAL, 32),
            occ_symbol=_safe_varchar(payload.get("occ_symbol"), 64),
            put_call=_safe_varchar(payload.get("put_call"), 16),
            position_side=_safe_varchar("BUY", 16),
            strike=float(payload.get("strike") or 0.0),
            expiration=payload.get("expiration"),
            entry_price=float(payload.get("entry_price") or 0.0),
            target1=float(payload.get("take_profit") or 0.0),
            stop=float(payload.get("stop") or 0.0),
            confidence=confidence,
            score=float(payload.get("score") or 0.0),
            details_json=json.dumps({
                "market_analysis": payload.get("market_analysis"),
                "rationale": payload.get("trade_rationale"),
                "futures_symbol": ES_FUTURES,
                "interval_minutes": CHART_INTERVAL_MINUTES,
                "arm_trigger": payload.get("arm_trigger"),
                "take_profit": payload.get("take_profit"),
                "option_delta": payload.get("option_delta"),
                "option_spread": payload.get("option_spread"),
            }, default=str),
        )
        db.add(pick)
        db.flush()

        # ── Open trade ──
        ot = PaperSPXOpenTrade(
            user_id=int(user_id) if user_id else 0,
            pick_id=pick.id,
            underlying_symbol=_safe_varchar(SPX_CANONICAL, 32),
            occ_symbol=_safe_varchar(payload.get("occ_symbol") or "", 64),
            put_call=_safe_varchar(payload.get("put_call") or "", 16),
            position_side=_safe_varchar("BUY", 16),
            strike=float(payload.get("strike") or 0.0),
            expiration=payload.get("expiration"),
            quantity=int(qty),
            entry_price=float(payload.get("entry_price") or 0.0),
            planned_take_profit_1=float(payload.get("take_profit") or 0.0),   # TP1 at +$1.50
            planned_stop_loss=float(payload.get("stop") or 0.0),
            status="OPEN",
            opened_at=datetime.utcnow(),
        )
        # Store v4 state in details for manage loop (arm + lock-in + time-stop)
        ot_details = {
            "arm_trigger": payload.get("arm_trigger"),
            "take_profit": payload.get("take_profit"),
            "lock_in_trigger": payload.get("lock_in_trigger"),
            "lock_in_floor": payload.get("lock_in_floor"),
            "lock_in_applied": False,
            "edge_ratio": payload.get("edge_ratio"),
            "expected_move_5min": payload.get("expected_move_5min"),
            "edge_state": (payload.get("market_analysis") or {}).get("edge_state"),
            "opened_at_utc": datetime.utcnow().isoformat(),
        }
        ot.details_json = json.dumps(ot_details, default=str) \
            if hasattr(ot, "details_json") else None

        db.add(ot)
        db.commit()

        _write_spx0dte_status(
            user_id,
            action="TRADE_OPENED",
            signal=payload.get("put_call", "NONE"),
            reason=payload.get("trade_rationale") or "Trade opened",
            es_data=(payload.get("market_analysis") or {}),
            risk=risk,
            extra={
                "open_trade_id": getattr(ot, "id", None),
                "pick_id": getattr(pick, "id", None),
                "occ_symbol": payload.get("occ_symbol"),
                "strike": payload.get("strike"),
                "entry_price": payload.get("entry_price"),
                "take_profit": payload.get("take_profit"),
                "stop": payload.get("stop"),
                "confidence": confidence,
                "quantity": qty,
            },
        )

        try:
            _send_spx_open_alerts(ot)
        except Exception as e:
            log.warning("[SPX0DTE] Failed sending open alerts for trade_id=%s: %s", getattr(ot, "id", None), e)

        log.info(
            "[SPX0DTE] ✅ Trade opened | id=%s occ=%s entry=%.2f stop=%.2f "
            "TP1=%.2f arm=%.2f confidence=%.1f",
            ot.id, ot.occ_symbol, ot.entry_price,
            float(payload.get("stop", 0)),
            float(payload.get("take_profit", 0)),
            float(payload.get("arm_trigger", 0)),
            confidence
        )

        return {
            "ok": True,
            "opened": True,
            "open_trade_id": ot.id,
            "pick_id": pick.id,
            "confidence": confidence,
            "entry": float(payload["entry_price"]),
            "stop": float(payload["stop"]),
            "take_profit": float(payload["take_profit"]),
            "arm_trigger": float(payload["arm_trigger"]),
            "max_risk_usd": round((float(payload["entry_price"]) - float(payload["stop"])) * 100 * qty, 2),
            "target_profit_usd": round((float(payload["take_profit"]) - float(payload["entry_price"])) * 100 * qty, 2),
            "interval_minutes": CHART_INTERVAL_MINUTES,
            "futures_symbol": ES_FUTURES,
        }

    except Exception as e:
        db.rollback()
        log.exception("run_spx0dte_tick failed: %s", e)
        _write_spx0dte_status(user_id, action="ERROR", signal="NONE", reason=str(e))
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


# ─── Mark / Exit Quote Helpers ────────────────────────────────────────────────

def _get_mark_for_occ(occ_symbol: str, exp_date: Optional[date] = None) -> Optional[float]:
    exp = exp_date or _today_et()
    occ = _normalize_occ(occ_symbol)

    for root in [SPX_WEEKLY, SPX_SCHWAB, SPX_CANONICAL]:
        df, _ = _fetch_option_chain_native(root, exp)
        if df is None or df.empty or "symbol" not in df.columns:
            continue

        df2 = df.copy()
        df2["_sym"] = df2["symbol"].astype(str).str.replace(" ", "", regex=False).str.strip()
        row = df2[df2["_sym"] == occ]
        if row.empty:
            row = df2[df2["_sym"].str.contains(occ, regex=False)]
        if row.empty:
            continue

        r0 = row.iloc[0]
        bid = _as_float(r0.get("bid"), 0.0)
        ask = _as_float(r0.get("ask"), 0.0)
        mark = _as_float(r0.get("mark"), 0.0)
        last = _as_float(r0.get("last"), 0.0)

        if mark > 0:
            return round(mark, 2)
        if last > 0:
            return round(last, 2)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2.0, 2)
        if bid > 0:
            return round(bid, 2)
        if ask > 0:
            return round(ask, 2)

    log.warning("[SPX0DTE] mark lookup failed | occ=%s exp=%s", occ, exp)
    return None


def _get_exit_quote_for_occ(occ_symbol: str, exp_date: Optional[date] = None) -> Optional[Dict[str, float]]:
    exp = exp_date or _today_et()
    occ = _normalize_occ(occ_symbol)

    for root in [SPX_WEEKLY, SPX_SCHWAB, SPX_CANONICAL]:
        df, _ = _fetch_option_chain_native(root, exp)
        if df is None or df.empty or "symbol" not in df.columns:
            continue

        df2 = df.copy()
        df2["_sym"] = df2["symbol"].astype(str).str.replace(" ", "", regex=False).str.strip()
        row = df2[df2["_sym"] == occ]
        if row.empty:
            row = df2[df2["_sym"].str.contains(occ, regex=False)]
        if row.empty:
            continue

        r0 = row.iloc[0]
        return {
            "bid": round(_as_float(r0.get("bid"), 0.0), 2),
            "ask": round(_as_float(r0.get("ask"), 0.0), 2),
            "mark": round(_as_float(r0.get("mark"), 0.0), 2),
            "last": round(_as_float(r0.get("last"), 0.0), 2),
        }

    log.warning("[SPX0DTE] exit quote lookup failed | occ=%s exp=%s", occ, exp)
    return None


def _choose_honest_exit_price_for_long(
    quote: Optional[Dict[str, float]],
    fallback_mark: Optional[float] = None,
) -> Optional[float]:
    """
    For long options: BID is the honest exit (what we'd actually get selling).
    Falls back to MARK then LAST if no bid.
    """
    if quote:
        bid = _as_float(quote.get("bid"), 0.0)
        mark = _as_float(quote.get("mark"), 0.0)
        last = _as_float(quote.get("last"), 0.0)
        if bid > 0:
            return round(bid, 2)
        if mark > 0:
            return round(mark, 2)
        if last > 0:
            return round(last, 2)

    if fallback_mark is not None and _as_float(fallback_mark, 0.0) > 0:
        return round(float(fallback_mark), 2)
    return None


# ─── Trade Close ──────────────────────────────────────────────────────────────

def close_spx_trade(
    db,
    ot: PaperSPXOpenTrade,
    exit_price: float,
    reason: str,
    details: Optional[dict] = None
) -> PaperSPXTradeHistory:
    qty = int(ot.quantity or 1)
    entry = float(ot.entry_price or 0.0)
    exitp = float(exit_price or 0.0)

    pnl_points = exitp - entry
    pnl_usd = (pnl_points * 100.0 * qty) - (2.50 * qty)  # deduct $2.50/contract commission

    merged_details = dict(details or {})
    merged_details.setdefault("planned_stop_loss", _maybe_float(getattr(ot, "planned_stop_loss", None)))
    merged_details.setdefault("planned_take_profit_1", _maybe_float(getattr(ot, "planned_take_profit_1", None)))

    hist = PaperSPXTradeHistory(
        user_id=ot.user_id,
        underlying_symbol=ot.underlying_symbol,
        occ_symbol=ot.occ_symbol,
        put_call=ot.put_call,
        position_side=ot.position_side or "BUY",
        strike=ot.strike,
        expiration=ot.expiration,
        quantity=qty,
        entry_price=entry,
        exit_price=exitp,
        opened_at=ot.opened_at,
        closed_at=datetime.utcnow(),
        close_reason=reason,
        pnl_points=round(float(pnl_points), 2),
        pnl_usd=round(float(pnl_usd), 2),
        pick_id=ot.pick_id,
        details_json=json.dumps(merged_details, default=str),
    )
    db.add(hist)
    db.flush()

    try:
        _send_spx_close_alerts(hist)
    except Exception as e:
        log.warning("[SPX0DTE] Failed sending close alerts for trade_id=%s: %s", getattr(hist, "id", None), e)

    db.delete(ot)
    return hist


# ─── Trade Management Loop ────────────────────────────────────────────────────

def manage_spx0dte_open_trades(user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    v4 manage loop:
      0. Fetch BID + mark together
      1. Time stop: exit if flat after TIME_STOP_MINUTES
      2. TP1 hit (tightened slippage tolerance to 0.95)
      3. Lock-in: when mark >= LOCK_IN_TRIGGER, raise stop to LOCK_IN_FLOOR
      4. Arm + update trailing stop (tighter TRAIL_GAP_POINTS)
      5. Stop loss check (uses BID, not mark — eliminates intra-poll slippage)
      6. EOD exit
    """
    db = SessionLocal()
    now = _now_et()
    log.info("[v4] Managing open trades at %s", now.isoformat())

    try:
        q = db.query(PaperSPXOpenTrade).filter(PaperSPXOpenTrade.status == "OPEN")
        if user_id:
            q = q.filter(PaperSPXOpenTrade.user_id == user_id)
        open_trades = q.all()

        if not open_trades:
            return {"ok": True, "open_trades": 0}

        stats = {
            "updated": 0, "armed": 0, "trailed": 0, "tp_hits": 0,
            "lock_ins": 0, "time_stops": 0, "closed": 0, "skipped": 0,
        }

        for ot in open_trades:
            expd = ot.expiration.date() if hasattr(ot.expiration, "date") else ot.expiration

            # Fetch quote (bid+mark) together — used for both stop check and exit
            exit_quote = _get_exit_quote_for_occ(ot.occ_symbol, exp_date=expd)
            if exit_quote is None:
                stats["skipped"] += 1
                log.warning("[v4] No quote for %s — skipping", ot.occ_symbol)
                continue

            bid = float(exit_quote.get("bid", 0.0) or 0.0)
            ask = float(exit_quote.get("ask", 0.0) or 0.0)
            mark = float(exit_quote.get("mark", 0.0) or 0.0)
            last = float(exit_quote.get("last", 0.0) or 0.0)

            if mark <= 0:
                if bid > 0 and ask > 0:
                    mark = (bid + ask) / 2.0
                elif last > 0:
                    mark = last
                elif bid > 0:
                    mark = bid
                else:
                    stats["skipped"] += 1
                    continue

            # Stop reference uses BID (what we'd actually fill at)
            stop_ref = bid if bid > 0 else mark

            ot.current_mark_price = round(mark, 2)
            stats["updated"] += 1

            entry = float(ot.entry_price or 0.0)
            stop = float(ot.planned_stop_loss or 0.0)
            tp1 = float(ot.planned_take_profit_1 or 0.0)

            # Recover v4 state from details
            details = {}
            try:
                if hasattr(ot, "details_json") and ot.details_json:
                    details = json.loads(ot.details_json) or {}
            except Exception:
                pass

            arm_trigger = float(details.get("arm_trigger", entry + ARM_PROFIT_POINTS))
            lock_in_trigger = float(details.get("lock_in_trigger", entry + LOCK_IN_TRIGGER_POINTS))
            lock_in_floor = float(details.get("lock_in_floor", entry + LOCK_IN_FLOOR_POINTS))
            lock_in_applied = bool(details.get("lock_in_applied", False))

            # Elapsed time
            opened_utc = ot.opened_at
            if opened_utc is not None:
                if opened_utc.tzinfo is None:
                    opened_utc = opened_utc.replace(tzinfo=UTC)
                elapsed_min = (datetime.now(UTC) - opened_utc).total_seconds() / 60.0
            else:
                elapsed_min = 0.0

            is_armed = mark >= arm_trigger

            log.info(
                "[v4] Manage | occ=%s entry=%.2f bid=%.2f mark=%.2f stop=%.2f "
                "TP=%.2f arm=%.2f lock@%.2f→%.2f armed=%s lock_in=%s elapsed=%.1fm",
                ot.occ_symbol, entry, bid, mark, stop, tp1, arm_trigger,
                lock_in_trigger, lock_in_floor, is_armed, lock_in_applied, elapsed_min
            )

            # ── Step 1: Time stop ────────────────────────────────────────────
            if (elapsed_min >= TIME_STOP_MINUTES
                    and (mark - entry) < TIME_STOP_MIN_PROFIT
                    and not is_armed):
                exit_price = _choose_honest_exit_price_for_long(exit_quote, fallback_mark=mark)
                if exit_price is None:
                    stats["skipped"] += 1
                    continue
                log.info(
                    "[v4] ⏱ TIME_STOP | occ=%s elapsed=%.1fm mark=%.2f exit=%.2f",
                    ot.occ_symbol, elapsed_min, mark, exit_price
                )
                hist = close_spx_trade(
                    db=db, ot=ot, exit_price=float(exit_price), reason="TIME_STOP",
                    details={"elapsed_minutes": round(elapsed_min, 2),
                             "observed_mark": mark, "observed_bid": bid,
                             "actual_exit": exit_price, "quote_snapshot": exit_quote}
                )
                stats["closed"] += 1
                stats["time_stops"] += 1
                log.info("[v4] Time-stopped | pnl=$%.2f", float(hist.pnl_usd or 0))
                continue

            # ── Step 2: TP hit ───────────────────────────────────────────────
            if tp1 > 0 and mark >= tp1:
                exit_price = _choose_honest_exit_price_for_long(exit_quote, fallback_mark=mark)
                if exit_price and exit_price >= tp1 * TP_SLIP_TOLERANCE:
                    log.info(
                        "[v4] 🎯 TP HIT | occ=%s mark=%.2f tp=%.2f exit=%.2f profit=+$%.0f",
                        ot.occ_symbol, mark, tp1, exit_price,
                        (exit_price - entry) * 100 * int(ot.quantity or 1)
                    )
                    hist = close_spx_trade(
                        db=db, ot=ot, exit_price=float(exit_price), reason="TP1",
                        details={"tp1_level": tp1, "observed_mark": mark,
                                 "observed_bid": bid, "actual_exit": exit_price,
                                 "quote_snapshot": exit_quote}
                    )
                    stats["closed"] += 1
                    stats["tp_hits"] += 1
                    log.info("[v4] TP closed | pnl=$%.2f", float(hist.pnl_usd or 0))
                    continue

            # ── Step 3: Lock-in (raise stop once profitable) ─────────────────
            if not lock_in_applied and mark >= lock_in_trigger:
                if lock_in_floor > stop:
                    ot.planned_stop_loss = round(lock_in_floor, 2)
                    stop = float(ot.planned_stop_loss)
                    details["lock_in_applied"] = True
                    ot.details_json = json.dumps(details, default=str)
                    stats["lock_ins"] += 1
                    log.info("[v4] 🔒 LOCK-IN | occ=%s mark=%.2f stop→%.2f (locks +$%.0f)",
                             ot.occ_symbol, mark, stop, (stop - entry) * 100)

            # ── Step 4: Arm + trailing stop ──────────────────────────────────
            if is_armed:
                stats["armed"] += 1
                new_stop = round(max(stop, mark - TRAIL_GAP_POINTS), 2)
                if new_stop > stop:
                    ot.planned_stop_loss = new_stop
                    stop = new_stop
                    stats["trailed"] += 1
                    log.info("[v4] Trail | occ=%s new_stop=%.2f mark=%.2f",
                             ot.occ_symbol, new_stop, mark)

            # ── Step 5: Stop loss (BID-based check) ──────────────────────────
            reason = None
            exit_price = None

            if stop > 0 and stop_ref > 0 and stop_ref <= stop:
                reason = "TRAIL_SL" if is_armed or lock_in_applied else "SL"
                exit_price = _choose_honest_exit_price_for_long(exit_quote, fallback_mark=mark)
                if exit_price is None:
                    stats["skipped"] += 1
                    log.warning("[v4] Stop hit but no exit price | occ=%s", ot.occ_symbol)
                    continue

                slippage = stop - exit_price
                log.info(
                    "[v4] Stop | occ=%s reason=%s stop=%.2f bid=%.2f exit=%.2f slip=$%.0f",
                    ot.occ_symbol, reason, stop, bid, exit_price, slippage * 100
                )

            # ── Step 6: EOD exit ─────────────────────────────────────────────
            if reason is None and now.time() >= EOD_EXIT_TIME:
                reason = "EOD"
                exit_price = _choose_honest_exit_price_for_long(exit_quote, fallback_mark=mark)
                if exit_price is None:
                    stats["skipped"] += 1
                    continue
                log.info("[v4] EOD close | occ=%s mark=%.2f exit=%.2f",
                         ot.occ_symbol, mark, exit_price)

            if reason and exit_price is not None:
                hist = close_spx_trade(
                    db=db, ot=ot, exit_price=float(exit_price), reason=reason,
                    details={"trigger_stop": stop, "stop_ref": stop_ref,
                             "observed_mark": mark, "observed_bid": bid,
                             "actual_exit": exit_price, "quote_snapshot": exit_quote,
                             "is_armed": is_armed, "lock_in_applied": lock_in_applied}
                )
                stats["closed"] += 1
                log.info("[v4] Closed | reason=%s exit=%.2f pnl=$%.2f",
                         reason, float(exit_price), float(hist.pnl_usd or 0))

        db.commit()

        result = {
            "ok": True,
            "open_trades": len(open_trades),
            "now_et": now.isoformat(),
            **stats,
        }
        log.info("[v4] Management complete: %s", result)
        return result

    except Exception as e:
        db.rollback()
        log.exception("[v4] manage_spx0dte_open_trades failed: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        db.close()


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SPX 0DTE SMI Reversal v2 — $100 scalps, buy-only"
    )
    parser.add_argument("--user-id", type=int, default=0)
    parser.add_argument("--max-risk", type=float, default=100.0,
                        help="Max dollar risk per contract (default $100)")
    parser.add_argument("--manage", action="store_true",
                        help="Run trade management loop (call every minute)")
    parser.add_argument("--test-after-hours", action="store_true")
    parser.add_argument("--force-run-date", type=str, default="")
    parser.add_argument("--lenient", action="store_true",
                        help="Disable strict SMI level cross requirement")
    parser.add_argument("--interval", type=int, choices=[1, 5], default=1,
                        help="Chart interval in minutes (1 or 5)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)
        for h in log.handlers:
            h.setLevel(logging.DEBUG)

    if args.test_after_hours:
        TEST_AFTER_HOURS = True
        os.environ["SPX0DTE_TEST_AFTER_HOURS"] = "1"
    if args.force_run_date:
        os.environ["SPX0DTE_FORCE_RUN_DATE"] = args.force_run_date
    if args.lenient:
        STRICT_LEVEL_CROSS = False

    CHART_INTERVAL_MINUTES = args.interval

    if args.manage:
        result = manage_spx0dte_open_trades(user_id=args.user_id or None)
    else:
        result = run_spx0dte_tick(
            user_id=args.user_id or None,
            max_risk=args.max_risk,
            interval_minutes=args.interval,
        )

    print(json.dumps(result, indent=2, default=str))