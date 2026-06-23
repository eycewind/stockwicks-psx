#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/base_wiring.py

"""Interval-aware base wiring for StockWicks algos.

Changes vs old version:
  1) is_market_open_now() — proper RTH gate (Mon–Fri 9:25–16:05 ET).
     Old version used 00:00–23:55 which let post-market Celery ticks fetch from Schwab.
     algoMM_runner also has its own early-exit gate for belt-and-suspenders.
  2) max_bar_age_for_interval — tightened for 5min (8 min) and 1min (3 min).
  3) fetch_source_bars — now accepts lookback_days param so algoMM_runner
     can request 60 days without a second function being needed.
  4) once_per_bar_guard — unchanged; used by algoMM_runner via runner instance.
"""

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, time as dtime
from typing import Optional

import pandas as pd
import pytz
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot, PaperStockBotOpenTrade
from app.services.trade_service import place_paper_and_maybe_live_order
from app.utils.schwab_circuit import schwab_get
from app.utils.stock.schwab_token import get_valid_access_token

_ET = pytz.timezone("US/Eastern")

# Module-level once-per-bar guard (shared across all StockBaseRunner instances in this process)
_LAST_BAR_TS: dict[int, str] = {}


# ──────────────────────────────────────────────
# Time / session helpers
# ──────────────────────────────────────────────

def is_market_open_now(now_et: Optional[datetime] = None) -> bool:
    """
    Returns True only during equity RTH + a small buffer for pre/post-bar processing.
    Mon–Fri, 9:25 AM – 4:05 PM ET.

    The 5-minute buffer on each side ensures:
      - Pre-open (9:25): anchor computation and data fetch before 9:30 bars
      - Post-close (4:05): EOD bar processing after 4:00 close
    """
    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return dtime(9, 25) <= t <= dtime(16, 5)


def max_bar_age_for_interval(interval: str) -> timedelta:
    """
    Maximum acceptable age for the most-recent bar before declaring STALE.
    Kept intentionally tight to catch Schwab data lag early.
    """
    interval = (interval or "1min").lower()
    return {
        "1min":  timedelta(minutes=3),
        "5min":  timedelta(minutes=8),   # was 12 in old wiring — tightened
        "10min": timedelta(minutes=16),
        "15min": timedelta(minutes=25),
        "30min": timedelta(minutes=45),
        "1h":    timedelta(minutes=90),
        "1d":    timedelta(days=2),
        "1wk":   timedelta(days=14),
    }.get(interval, timedelta(minutes=10))


# ──────────────────────────────────────────────
# Data processing
# ──────────────────────────────────────────────

def process_interval(df: pd.DataFrame, interval: str, symbol: str):
    tz = _ET
    if df is None or df.empty:
        logging.warning("[BOT] Empty DF for %s", symbol)
        return None, None, None
    try:
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(tz)
    except Exception as e:
        logging.warning("[BOT] %s tz/parse error: %s", symbol, e)
        return None, None, None

    rule_map = {
        "1min": "1min", "5min": "5min", "10min": "10min",
        "15min": "15min", "30min": "30min",
        "1h": "1H", "1d": "1D", "1wk": "1W",
    }
    rule = rule_map.get((interval or "1min").lower())
    if not rule:
        logging.warning("[BOT] Unsupported interval '%s' for %s", interval, symbol)
        return None, None, None

    resampled = (
        df.resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    if resampled.empty:
        logging.warning("[BOT] Resampled empty for %s@%s", symbol, interval)
        return None, None, None

    # Keep only regular-session bars for intraday intervals
    if rule not in ("1D", "1W"):
        resampled = resampled.between_time("09:30", "16:00")
    if resampled.empty:
        logging.warning("[BOT] Empty after session filter for %s@%s", symbol, interval)
        return None, None, None

    latest_time = resampled.index[-1]
    return latest_time, None, resampled


# ──────────────────────────────────────────────
# Schwab helpers
# ──────────────────────────────────────────────

_MINUTE_FREQS     = {"1min": 1, "5min": 5, "10min": 10, "15min": 15, "30min": 30}
_SUPPORTED_INTERVALS = {"1min", "5min", "10min", "15min", "30min", "1h", "1d", "1wk"}
_SCHWAB_RATE_LOCK = threading.Lock()
_SCHWAB_REQUEST_TIMES: deque[float] = deque()


class SchwabAuthError(RuntimeError):
    """Raised when Schwab market-data credentials are rejected."""


def _caller_stack_if_enabled() -> Optional[str]:
    if os.getenv("STOCKWICKS_DEBUG_SCHWAB_STACK", "0") != "1":
        return None
    import traceback
    return "".join(traceback.format_stack(limit=25))


def _throttle_schwab_pricehistory() -> None:
    max_tps = float(os.getenv("SCHWAB_PRICEHISTORY_MAX_TPS", "50") or 0)
    if max_tps <= 0:
        return

    while True:
        wait_sec = 0.0
        now = time.monotonic()
        with _SCHWAB_RATE_LOCK:
            while _SCHWAB_REQUEST_TIMES and now - _SCHWAB_REQUEST_TIMES[0] >= 1.0:
                _SCHWAB_REQUEST_TIMES.popleft()
            if len(_SCHWAB_REQUEST_TIMES) < max_tps:
                _SCHWAB_REQUEST_TIMES.append(now)
                return
            wait_sec = max(0.001, 1.0 - (now - _SCHWAB_REQUEST_TIMES[0]))
        time.sleep(wait_sec)


def _fetch_schwab_pricehistory(
    *,
    symbol: str,
    frequency_type: str,
    frequency: int,
    start_ms: int,
    end_ms: int,
    user_id: int | None = None,
    need_extended_hours: bool = False,
    timeout_sec: int = 10,
) -> list[dict]:
    access_token = get_valid_access_token(user_id)
    if not access_token:
        raise RuntimeError("No valid Schwab access token")

    url = "https://api.schwabapi.com/marketdata/v1/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "frequencyType": frequency_type,
        "frequency": frequency,
        "startDate": int(start_ms),
        "endDate": int(end_ms),
        "needExtendedHoursData": "true" if need_extended_hours else "false",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    _throttle_schwab_pricehistory()
    resp = schwab_get(url, headers=headers, params=params, timeout=timeout_sec)
    if resp.status_code == 401:
        logging.warning("[SCHWAB] pricehistory 401 for %s; refreshing market token and retrying once", symbol.upper())
        access_token = get_valid_access_token(user_id, force_refresh=True)
        if not access_token:
            raise SchwabAuthError("Schwab market token refresh failed after pricehistory 401. Reconnect Schwab Market Data.")

        headers = {"Authorization": f"Bearer {access_token}"}
        _throttle_schwab_pricehistory()
        resp = schwab_get(url, headers=headers, params=params, timeout=timeout_sec)
        if resp.status_code == 401:
            raise SchwabAuthError("Schwab market-data token was rejected after refresh. Reconnect Schwab Market Data.")

    if resp.status_code == 400:
        return []
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("candles") or []


def get_schwab_history(
    symbol: str,
    interval: str,
    *,
    lookback_days: int = 7,
    user_id: int | None = None,
    debug_stack_on_error: bool = False,
    need_extended_hours: bool = False,
    raise_on_empty: bool = False,
) -> pd.DataFrame:
    """
    Fetch Schwab price history for the given interval.

    lookback_days controls how many calendar days back to fetch.
    For algoMM bots pass cfg.builder_days (typically 60).
    """
    interval = (interval or "1min").lower()
    if interval not in _SUPPORTED_INTERVALS:
        logging.warning("[SCHWAB] Unsupported interval '%s', defaulting to 1min", interval)
        interval = "1min"

    if interval in _MINUTE_FREQS:
        fetch_interval = interval
        freq_type = "minute"
        freq = _MINUTE_FREQS[interval]
        session_start = (9, 30)
        session_end   = (16, 0)
    elif interval == "1h":
        fetch_interval = "30min"
        freq_type = "minute"
        freq = _MINUTE_FREQS["30min"]
        session_start = (9, 30)
        session_end   = (16, 0)
    else:
        fetch_interval = "1d"
        freq_type = "daily"
        freq = 1
        session_start = (0, 0)
        session_end   = (23, 59)

    eastern = _ET
    all_dfs: list[pd.DataFrame] = []
    errors: list[str] = []
    empty_days = 0
    today = pd.Timestamp.now(tz=eastern).date()
    max_scan_days = int(max(lookback_days, 7) * 1.8)

    for days_back in range(max_scan_days):
        day = today - pd.Timedelta(days=days_back)
        if day.weekday() >= 5:
            continue

        start_dt = pd.Timestamp(day, tz=eastern).replace(
            hour=session_start[0], minute=session_start[1]
        )
        end_dt = pd.Timestamp(day, tz=eastern).replace(
            hour=session_end[0], minute=session_end[1]
        )

        try:
            candles = _fetch_schwab_pricehistory(
                symbol=symbol,
                frequency_type=freq_type,
                frequency=freq,
                start_ms=int(start_dt.timestamp() * 1000),
                end_ms=int(end_dt.timestamp() * 1000),
                user_id=user_id,
                need_extended_hours=need_extended_hours,
            )
            if not candles:
                empty_days += 1
                continue

            day_df = pd.DataFrame(candles)
            if "datetime" in day_df.columns:
                day_df["timestamp"] = (
                    pd.to_datetime(day_df["datetime"], unit="ms", utc=True)
                    .dt.tz_convert(eastern)
                )
                day_df = day_df.set_index("timestamp")

            keep = {"open", "high", "low", "close", "volume"}
            if not keep.issubset(set(day_df.columns)):
                continue

            all_dfs.append(day_df[["open", "high", "low", "close", "volume"]])
            day_sleep = float(os.getenv("SCHWAB_HISTORY_DAY_SLEEP_SEC", "0") or 0)
            if day_sleep > 0:
                time.sleep(day_sleep)

            if len(all_dfs) >= lookback_days:
                break

        except SchwabAuthError:
            raise
        except Exception as e:
            errors.append(str(e))
            logging.warning("[SCHWAB] fetch error %s@%s: %s", symbol, fetch_interval, e)
            if debug_stack_on_error:
                stack = _caller_stack_if_enabled()
                if stack:
                    logging.warning("CALLER STACK:\n%s", stack)
            continue

    if not all_dfs:
        if raise_on_empty:
            unique_errors = []
            for err in errors:
                if err not in unique_errors:
                    unique_errors.append(err)
            details = "; ".join(unique_errors[:3])
            if details:
                raise RuntimeError(
                    f"No Schwab candles for {symbol.upper()} {interval} after scanning "
                    f"{max_scan_days} calendar days ({empty_days} empty responses). Last errors: {details}"
                )
            raise RuntimeError(
                f"No Schwab candles for {symbol.upper()} {interval} after scanning "
                f"{max_scan_days} calendar days ({empty_days} empty responses). "
                "Check Schwab market-data access, token freshness, symbol support, or reduce History Days."
            )
        return pd.DataFrame()

    df_all = pd.concat(all_dfs).sort_index()
    df_all = df_all[~df_all.index.duplicated(keep="first")]

    if freq_type == "minute":
        df_all = df_all.between_time("09:30", "16:00")

    return df_all


# ──────────────────────────────────────────────
# Runner base class
# ──────────────────────────────────────────────

class StockBaseRunner:
    """Shared wiring for all stock algos."""

    def get_db(self) -> Session:
        return SessionLocal()

    def now_et(self) -> datetime:
        return datetime.now(_ET)

    def load_bot(self, db: Session, bot_id: int) -> Optional[PaperStockTradeBot]:
        return db.query(PaperStockTradeBot).filter_by(id=bot_id).first()

    def is_market_open_now(self) -> bool:
        return is_market_open_now(self.now_et())

    def fetch_source_bars(
        self,
        symbol: str,
        interval: str = "1min",
        lookback_days: Optional[int] = None,
        user_id: int | None = None,
        raise_on_empty: bool = False,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV bars from Schwab.
        lookback_days defaults to a sensible per-interval value if not supplied.
        algoMM passes cfg.builder_days (60) here so the runner has enough
        history for model training in one shot.
        """
        debug_stack = os.getenv("STOCKWICKS_DEBUG_SCHWAB_STACK", "0") == "1"
        iv = (interval or "1min").lower()
        if lookback_days is None:
            lookback_days = {
                "1min": 30, "5min": 30, "10min": 30,
                "15min": 30, "30min": 30, "1d": 365,
            }.get(iv, 7)
        return get_schwab_history(
            symbol, iv,
            lookback_days=lookback_days,
            user_id=user_id,
            debug_stack_on_error=debug_stack,
            need_extended_hours=False,
            raise_on_empty=raise_on_empty,
        )

    def csv_fallback(self, user_id: int, symbol: str, interval: str) -> Optional[pd.DataFrame]:
        try:
            from app.utils.stock.fetch_single_interval import fetch_if_needed
            chosen = (interval or "1min").lower()
            fetch_if_needed(user_id, symbol, chosen, force=True)
            user_dir = f"/var/www/stockwicks/data/{user_id}"
            csv_path = os.path.join(user_dir, f"{user_id}_{symbol}_{chosen}_data.csv")
            if os.path.exists(csv_path):
                df_csv = pd.read_csv(
                    csv_path, index_col="datetime", parse_dates=True
                ).sort_index()
                needed = {"open", "high", "low", "close", "volume"}
                if needed.issubset(df_csv.columns):
                    if getattr(df_csv.index, "tz", None) is None:
                        df_csv.index = pd.to_datetime(df_csv.index).tz_localize(_ET)
                    return df_csv[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            logging.error("[CSV] fallback error %s@%s: %s", symbol, interval, e)
        return None

    def resample_interval(self, df: pd.DataFrame, interval: str, symbol: str):
        chosen = (interval or "1min").lower()
        if chosen not in _SUPPORTED_INTERVALS:
            logging.warning("[BOT] Coercing unsupported interval '%s' -> '1min' for %s", interval, symbol)
            chosen = "1min"
        return process_interval(df, chosen, symbol)

    def max_age_for_interval(self, interval: str) -> timedelta:
        return max_bar_age_for_interval(interval)

    def once_per_bar_guard(self, bot_id: int, latest_bar_ts) -> bool:
        """
        Returns True (skip) if we already processed this bar for this bot.
        State lives in the module-level _LAST_BAR_TS dict — persists within
        a single Celery worker process lifetime.
        """
        iso = str(latest_bar_ts)
        last = _LAST_BAR_TS.get(bot_id)
        if last == iso:
            return True
        _LAST_BAR_TS[bot_id] = iso
        return False

    def get_open_trade(self, db: Session, bot_id: int, user_id: int, symbol: str):
        return (
            db.query(PaperStockBotOpenTrade)
            .filter_by(bot_id=bot_id, user_id=user_id, symbol=symbol)
            .first()
        )

    def run_for_bot(self, bot_id: int):
        db = self.get_db()
        bot = self.load_bot(db, bot_id)
        if not bot:
            print(f"[StockBaseRunner] Bot {bot_id} not found")
            return
        self.run_algo(db, bot)  # implemented by subclass runner

    def place_paper_trade_and_bookkeep(
        self,
        *,
        user_id: int,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        bot_id: int,
        algo_name: str,
    ) -> bool:
        db = SessionLocal()
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            return False
        place_paper_and_maybe_live_order(
            db=db,
            bot=bot,
            side=side.upper(),
            order_type="MARKET",
            qty=qty,
            limit_price=None,
            time_in_force="DAY",
            extended_hours=False,
            mirror_live_override=None,
            symbol_override=symbol,
            actor=f"{algo_name}:{bot_id}",
        )
        return True
