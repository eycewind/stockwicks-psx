# app/tasks/schwab_tasks.py
"""
Commercial Schwab token refresh task for one client.

Refreshes BOTH:
- schwab_market_token.json
- schwab_trade_token.json

Important:
- Forces commercial DATA_DIR before importing token utility modules.
- Prevents old production defaults like /var/www/stockwicks/data from being used.
- Never logs raw Schwab tokens.
"""

import logging
import os
from pathlib import Path

from celery import shared_task

log = logging.getLogger("schwab_tasks")


def _client_root() -> str:
    if os.getenv("CLIENT_ROOT"):
        return os.environ["CLIENT_ROOT"]
    # app/tasks/schwab_tasks.py -> client root
    return str(Path(__file__).resolve().parents[2])


def _data_dir() -> str:
    return os.getenv("DATA_DIR", f"{_client_root()}/data")


def _force_commercial_env(user_id: int) -> None:
    """
    Force correct commercial paths before importing Schwab token utility modules.

    Some old utility modules may read DATA_DIR at import time, so this must run
    before importing refresh_market_token / refresh_user_trade_token.
    """
    client_root = _client_root()
    data_dir = _data_dir()

    os.environ["CLIENT_ROOT"] = client_root
    os.environ["DATA_DIR"] = data_dir
    os.environ["LOG_DIR"] = os.getenv("LOG_DIR", f"{client_root}/logs")
    os.environ["MODEL_DIR"] = os.getenv("MODEL_DIR", f"{client_root}/models")
    os.environ["SCHWAB_REFRESH_USER_ID"] = str(user_id)


def _token_file_status(user_id: int) -> dict:
    """
    Return token file paths and existence only.
    Does not read or expose raw tokens.
    """
    base = Path(_data_dir()) / str(user_id)
    market = base / "schwab_market_token.json"
    trade = base / "schwab_trade_token.json"

    return {
        "data_dir": str(Path(_data_dir())),
        "user_dir": str(base),
        "market_path": str(market),
        "market_exists": market.exists(),
        "market_mtime": market.stat().st_mtime if market.exists() else None,
        "trade_path": str(trade),
        "trade_exists": trade.exists(),
        "trade_mtime": trade.stat().st_mtime if trade.exists() else None,
    }


def _token_user_ids() -> list[int]:
    """
    Discover token-owning users from this client's data directory.
    Any numeric subdirectory with a Schwab market or trade token is refreshed.
    """
    base = Path(_data_dir())
    if not base.exists():
        log.warning("[AUTO-REFRESH] DATA_DIR does not exist: %s", base)
        return []

    user_ids: list[int] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not child.name.isdigit():
            continue
        if (child / "schwab_market_token.json").exists() or (child / "schwab_trade_token.json").exists():
            user_ids.append(int(child.name))

    return user_ids


@shared_task(
    name="app.tasks.schwab_tasks.auto_refresh_user_token",
    bind=True,
    max_retries=3,
)
def auto_refresh_user_token(self, user_id: int | None = None):
    """
    Refresh Schwab market + trade tokens for users discovered in this client's data dir.

    Expected files:
      {DATA_DIR}/{user_id}/schwab_market_token.json
      {DATA_DIR}/{user_id}/schwab_trade_token.json
    """
    users = [int(user_id)] if user_id is not None else _token_user_ids()
    if not users:
        fallback_user_id = int(os.getenv("SCHWAB_REFRESH_USER_ID", "1"))
        _force_commercial_env(fallback_user_id)
        log.warning("[AUTO-REFRESH] No Schwab token files found under data_dir=%s", _data_dir())
        return {"ok": True, "users": [], "data_dir": _data_dir(), "message": "no token files found"}

    # Import AFTER env is set below; old token modules read env at import time.
    first_user_id = users[0]
    _force_commercial_env(first_user_id)

    try:
        from app.utils.stock.schwab_market_token import refresh_market_token
        from app.utils.stock.schwab_trade_token import refresh_user_trade_token

        results = []
        for uid in users:
            _force_commercial_env(uid)
            before = _token_file_status(uid)

            log.info(
                "[AUTO-REFRESH] Starting Schwab token refresh user_id=%s data_dir=%s",
                uid,
                before["data_dir"],
            )
            log.info(
                "[AUTO-REFRESH] Token paths market=%s exists=%s trade=%s exists=%s",
                before["market_path"],
                before["market_exists"],
                before["trade_path"],
                before["trade_exists"],
            )

            market_result = None
            trade_result = None

            if before["market_exists"]:
                log.info("[AUTO-REFRESH] Refreshing market token for user_id=%s", uid)
                market_result = refresh_market_token(uid)

            if before["trade_exists"]:
                log.info("[AUTO-REFRESH] Refreshing trade token for user_id=%s", uid)
                trade_result = refresh_user_trade_token(uid)

            after = _token_file_status(uid)

            log.info(
                "[AUTO-REFRESH] Done user_id=%s market_mtime_before=%s market_mtime_after=%s trade_mtime_before=%s trade_mtime_after=%s",
                uid,
                before["market_mtime"],
                after["market_mtime"],
                before["trade_mtime"],
                after["trade_mtime"],
            )

            results.append(
                {
                    "user_id": uid,
                    "data_dir": after["data_dir"],
                    "market_path": after["market_path"],
                    "market_exists": after["market_exists"],
                    "market_mtime_before": before["market_mtime"],
                    "market_mtime_after": after["market_mtime"],
                    "trade_path": after["trade_path"],
                    "trade_exists": after["trade_exists"],
                    "trade_mtime_before": before["trade_mtime"],
                    "trade_mtime_after": after["trade_mtime"],
                    "market_result": str(market_result),
                    "trade_result": str(trade_result),
                }
            )

        return {"ok": True, "users": users, "data_dir": _data_dir(), "results": results}

    except Exception as exc:
        log.exception("[AUTO-REFRESH] Failed refreshing Schwab tokens for users=%s", users)
        raise self.retry(exc=exc, countdown=60)
