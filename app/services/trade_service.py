# app/services/trade_service.py
# app/services/trade_service.py

import os
import logging
from sqlalchemy.orm import Session
from app.database.connection import SessionLocal
from app.models.paper_trading_bot import PaperStockTradeBot
from app.utils.stock.paper_trade_engine import place_paper_trade  # confirmed path
from app.utils.stock.market_price import get_live_price

log = logging.getLogger(__name__)

# Global toggles
_MIRROR_ENV = os.getenv("MIRROR_TO_SCHWAB", "").strip().lower() in {"1", "true", "yes", "on"}
_ALLOW_MIRROR_WITHOUT_PAPER = os.getenv("ALLOW_MIRROR_WITHOUT_PAPER", "").strip().lower() in {"1", "true", "yes", "on"}

# --- Schwab submitter import resolver (robust) -------------------------------
_submit_import_errors: list[str] = []
submit_equity_order = None
for _path, _name in (
    ("app.utils.schwab_trade", "submit_equity_order"),      # preferred
    ("app.services.schwab_trade", "submit_equity_order"),   # alt
    ("app.utils.brokers.schwab", "submit_equity_order"),    # alt
    ("app.utils.schwab_trade", "place_equity_order"),       # variant
    ("app.utils.schwab_trade", "submit_order_equity"),      # variant
):
    try:
        _mod = __import__(_path, fromlist=[_name])
        submit_equity_order = getattr(_mod, _name)
        log.info(f"[TRADE] using {_path}.{_name} for Schwab submit")
        break
    except Exception as e:
        _submit_import_errors.append(f"{_path}.{_name}: {e!r}")

if submit_equity_order is None:
    log.warning(
        "[TRADE] No Schwab submitter found. Mirroring will be skipped.\n"
        + "\n".join(_submit_import_errors)
        + "\nProvide submit_equity_order(...) in one of the modules above."
    )

# --- Default Schwab account resolver -----------------------------------------
def _get_default_account_id(db: Session, user_id: int | None) -> str | None:
    """
    Resolve a Schwab account id/number to place live orders.
    Priority:
      1) Env var SCHWAB_ACCOUNT_ID
      2) User's default Schwab account (is_default = True)
      3) Any user's default Schwab account (is_default = True)
      4) First available Schwab account
    Returns the account_number (string) or None.
    """
    acct_env = os.getenv("SCHWAB_ACCOUNT_ID", "").strip()
    if acct_env:
        log.info(f"[TRADE] Using SCHWAB_ACCOUNT_ID from env: {acct_env}")
        return acct_env

    # Try to import your account model from likely locations
    SchwabAccount = None
    import_errors: list[str] = []
    for modpath in ("app.models.schwab_account", "app.models.schwab_accounts"):
        try:
            mod = __import__(modpath, fromlist=["SchwabAccount"])
            SchwabAccount = getattr(mod, "SchwabAccount")
            log.info(f"[TRADE] Using {modpath}.SchwabAccount")
            break
        except Exception as e:
            import_errors.append(f"{modpath}: {e!r}")

    if SchwabAccount is None:
        log.warning(
            "[TRADE] SchwabAccount model not found. "
            "Set SCHWAB_ACCOUNT_ID env var or ensure the model exists. "
            + " | ".join(import_errors)
        )
        return None

    q = db.query(SchwabAccount)
    if hasattr(SchwabAccount, "user_id") and user_id:
        q_user = q.filter(SchwabAccount.user_id == user_id)
        acct = q_user.filter(getattr(SchwabAccount, "is_default", False) == True).first() or q_user.first()
        if acct:
            return (
                getattr(acct, "account_hash", None)
                or getattr(acct, "account_number", None)
                or getattr(acct, "account_id", None)
            )

    acct = q.filter(getattr(SchwabAccount, "is_default", False) == True).first() or q.first()
    if not acct:
        log.warning("[TRADE] No Schwab accounts found in DB.")
        return None

    return (
        getattr(acct, "account_hash", None)
        or getattr(acct, "account_number", None)
        or getattr(acct, "account_id", None)
    )

# --- Helpers -----------------------------------------------------------------
def _normalize_paper_result(res) -> dict:
    """Make sure we always expose paper_trade_id + message."""
    out: dict = {}
    if isinstance(res, dict):
        out.update(res)
    else:
        try:
            out["paper_trade_id"] = getattr(res, "id", None) or getattr(res, "trade_id", None)
        except Exception:
            out["paper_trade_id"] = None
    if "paper_trade_id" not in out:
        for k in ("id", "trade_id", "paper_id"):
            if k in out:
                out["paper_trade_id"] = out[k]
                break
    return out

def _place_paper_trade_compat(
    *,
    db: Session,
    bot: PaperStockTradeBot,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    limit_price: float | None,
    time_in_force: str,
    extended_hours: bool,
    actor: str | None,
) -> dict:
    """
    Call place_paper_trade() regardless of its signature.
    Tries (in order):
      1) place_paper_trade(db=..., bot=..., ...)
      2) place_paper_trade(bot=..., ...)                  # no db
      3) place_paper_trade(bot_id=..., user_id=..., ...)  # id-based
      4) place_paper_trade(user_id, symbol, side, price, qty, bot_id=...)  # minimal engine
    Returns a normalized dict with paper_trade_id/message.
    """
    # 1) db + bot
    try:
        res = place_paper_trade(
            db=db,
            bot=bot,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            extended_hours=extended_hours,
            actor=actor,
        )
        return _normalize_paper_result(res)
    except TypeError:
        pass

    # 2) bot only
    try:
        res = place_paper_trade(
            bot=bot,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            extended_hours=extended_hours,
            actor=actor,
        )
        return _normalize_paper_result(res)
    except TypeError:
        pass

    # 3) id-based
    try:
        res = place_paper_trade(
            bot_id=bot.id,
            user_id=getattr(bot, "user_id", None),
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            extended_hours=extended_hours,
            actor=actor,
        )
        return _normalize_paper_result(res)
    except TypeError:
        pass

    # 4) minimal engine: (user_id, symbol, side, price, qty, bot_id=...)
    px = None
    if limit_price is not None:
        px = float(limit_price)
    else:
        try:
            live_px = get_live_price(symbol)
            if live_px is not None:
                px = float(live_px)
        except Exception:
            px = None
    if px is None:
        px = 0.0  # fallback to avoid Decimal(None) in engine

    res = place_paper_trade(
        getattr(bot, "user_id", None),
        symbol,
        side,
        px,
        float(qty),
        bot_id=bot.id,
    )
    return _normalize_paper_result(res)

# --- Main placement -----------------------------------------------------------
def place_paper_and_maybe_live_order(
    db: Session,
    bot: PaperStockTradeBot,
    side: str,
    order_type: str = "MARKET",
    qty: float = 1.0,
    limit_price: float | None = None,
    time_in_force: str = "DAY",
    extended_hours: bool = False,
    mirror_live_override: bool | None = None,
    symbol_override: str | None = None,
    actor: str | None = None,
    account_id_override: str | None = None,
    mirror_even_if_no_paper: bool = False,   # per-call override (bot/CLI)
):
    """
    1) Create the paper trade record (UI/history) via compat helper.
    2) If mirroring allowed -> submit a Schwab order.

    Mirroring policy:
      - If a new paper trade wasn't created, mirroring is blocked by default
        to avoid duplicates. You can override with:
          a) mirror_live_override=True  (force from caller)
          b) mirror_even_if_no_paper=True (per-call)
          c) ALLOW_MIRROR_WITHOUT_PAPER=1 (env, global)
    """
    symbol = (symbol_override or bot.symbol or "").upper()
    side_u = (side or "").upper()
    order_type_u = (order_type or "").upper()
    tif_u = (time_in_force or "DAY").upper()

    # ---- 1) PAPER TRADE ----
    paper_res = _place_paper_trade_compat(
        db=db,
        bot=bot,
        symbol=symbol,
        side=side_u,
        qty=qty,
        order_type=order_type_u,
        limit_price=limit_price,
        time_in_force=tif_u,
        extended_hours=extended_hours,
        actor=actor or "service",
    )
    created_paper = bool(paper_res.get("paper_trade_id"))

    # ---- 2) DECIDE MIRROR ----
    # Base policy: bot flag or env turns mirroring on
    mirror_live_flag = (
        (mirror_live_override is True) or
        bool(getattr(bot, "mirror_live", False)) or
        _MIRROR_ENV
    )

    # Safety gate: if no paper created, require an override to proceed
    if not created_paper and not (
        mirror_live_override is True or
        mirror_even_if_no_paper or
        _ALLOW_MIRROR_WITHOUT_PAPER
    ):
        log.info("[TRADE] Paper trade not created; mirroring blocked by safety gate "
                 "(set ALLOW_MIRROR_WITHOUT_PAPER=1 or pass mirror_even_if_no_paper=True).")
        mirror_live_flag = False

    log.info(
        f"[TRADE] mirror_decision bot_id={bot.id} created_paper={created_paper} "
        f"bot_flag={getattr(bot,'mirror_live',None)} override={mirror_live_override} "
        f"env_mirror={_MIRROR_ENV} env_allow_no_paper={_ALLOW_MIRROR_WITHOUT_PAPER} "
        f"call_allow_no_paper={mirror_even_if_no_paper} -> {mirror_live_flag}"
    )

    schwab_order_id = None
    if mirror_live_flag:
        try:
            account_id = account_id_override or _get_default_account_id(db, bot.user_id)
            if not account_id:
                raise RuntimeError("No Schwab account available for live mirroring.")

            if submit_equity_order is None:
                log.warning("[TRADE] submit_equity_order() not available; skipping live mirror.")
            else:
                if extended_hours and order_type_u == "MARKET":
                    log.warning("[TRADE] Converting MARKET→LIMIT for extended hours; provide limit_price.")
                    if limit_price is None:
                        raise ValueError("Provide a limit_price when trading in extended hours.")

                schwab_order_id = submit_equity_order(
                    account_id=account_id,
                    symbol=symbol,
                    side=side_u,
                    qty=qty,
                    order_type=order_type_u,
                    limit_price=limit_price,
                    time_in_force=tif_u,
                    extended_hours=extended_hours,
                )
                log.info(
                    f"[TRADE] Schwab order submitted ok id={schwab_order_id} "
                    f"acct={account_id} {symbol} {side_u} x{qty}"
                )

        except Exception as e:
            log.exception(f"[TRADE] Schwab order failed bot_id={bot.id} symbol={symbol}: {e}")

    return {
        "status": "submitted",
        "paper_trade_id": paper_res.get("paper_trade_id"),
        "schwab_order_id": schwab_order_id,
        "message": paper_res.get("message"),
    }

def execute_trade_signal(
    bot_id: int,
    side: str,
    order_type: str = "MARKET",
    qty: float = 1.0,
    limit_price: float | None = None,
    time_in_force: str = "DAY",
    extended_hours: bool = False,
    mirror_live_override: bool | None = None,
    symbol_override: str | None = None,
    actor: str | None = None,
    account_id_override: str | None = None,   # passthrough supported
    mirror_even_if_no_paper: bool = False,    # per-call override (bot/CLI)
):
    """
    Public gateway the UI, CLI, and all bot runners should call.
    """
    db = SessionLocal()
    try:
        bot = db.query(PaperStockTradeBot).filter_by(id=bot_id).first()
        if not bot:
            raise ValueError(f"Bot {bot_id} not found")

        return place_paper_and_maybe_live_order(
            db=db,
            bot=bot,
            side=side,
            order_type=order_type,
            qty=qty,
            limit_price=limit_price,
            time_in_force=time_in_force,
            extended_hours=extended_hours,
            mirror_live_override=mirror_live_override,
            symbol_override=symbol_override,
            actor=actor,
            account_id_override=account_id_override,
            mirror_even_if_no_paper=mirror_even_if_no_paper,
        )
    finally:
        db.close()
