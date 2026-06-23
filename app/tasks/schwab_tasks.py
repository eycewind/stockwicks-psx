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
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from celery import shared_task

log = logging.getLogger("schwab_tasks")
DEFAULT_REFRESH_SAFETY_SECONDS = 10 * 60
DEFAULT_BACKOFF_SECONDS = 60 * 60


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


def _token_payload(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def _token_expires_at(payload: dict) -> datetime | None:
    try:
        expires_in = int(payload.get("expires_in", 1800))
    except Exception:
        expires_in = 1800

    issued = (
        payload.get("token_time")
        or payload.get("created_at_epoch")
        or payload.get("created_at")
    )
    if issued is not None:
        try:
            return datetime.utcfromtimestamp(int(float(issued))) + timedelta(seconds=expires_in)
        except Exception:
            pass

    updated_at = payload.get("updated_at")
    if updated_at:
        try:
            return datetime.fromisoformat(str(updated_at).replace("Z", "+00:00")).replace(tzinfo=None) + timedelta(seconds=expires_in)
        except Exception:
            pass

    return None


def _token_needs_refresh(path: str | Path) -> bool:
    payload = _token_payload(path)
    if not payload.get("refresh_token"):
        return False
    if not payload.get("access_token"):
        return True

    expires_at = _token_expires_at(payload)
    if not expires_at:
        return True

    safety = int(os.getenv("SCHWAB_REFRESH_SAFETY_SECONDS", str(DEFAULT_REFRESH_SAFETY_SECONDS)))
    return datetime.utcnow() >= expires_at - timedelta(seconds=safety)


def _backoff_path(user_id: int, kind: str) -> Path:
    return Path(_data_dir()) / str(user_id) / f".schwab_{kind}_refresh_backoff.json"


def _backoff_active(user_id: int, kind: str) -> bool:
    path = _backoff_path(user_id, kind)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
        until = float(payload.get("until_epoch", 0))
    except Exception:
        return False
    if time.time() < until:
        log.warning("[AUTO-REFRESH] Skipping %s token refresh for user_id=%s due to cooldown until=%s", kind, user_id, until)
        return True
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def _set_backoff(user_id: int, kind: str, reason: str) -> None:
    seconds = int(os.getenv("SCHWAB_REFRESH_BACKOFF_SECONDS", str(DEFAULT_BACKOFF_SECONDS)))
    path = _backoff_path(user_id, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason[:200],
        "created_epoch": int(time.time()),
        "until_epoch": int(time.time() + seconds),
        "backoff_seconds": seconds,
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
    except Exception:
        log.warning("[AUTO-REFRESH] Could not write Schwab %s backoff marker for user_id=%s", kind, user_id, exc_info=True)


def _clear_backoff(user_id: int, kind: str) -> None:
    try:
        _backoff_path(user_id, kind).unlink(missing_ok=True)
    except Exception:
        pass


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
        failures = []
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
                if _backoff_active(uid, "market"):
                    market_result = "skipped_backoff"
                elif not _token_needs_refresh(before["market_path"]):
                    market_result = "skipped_not_due"
                    log.info("[AUTO-REFRESH] Market token not due for refresh user_id=%s", uid)
                else:
                    log.info("[AUTO-REFRESH] Refreshing market token for user_id=%s", uid)
                    market_result = refresh_market_token(uid)
                    if market_result is None:
                        failures.append({"user_id": uid, "kind": "market", "path": before["market_path"]})
                        _set_backoff(uid, "market", "refresh returned None")
                    else:
                        _clear_backoff(uid, "market")

            if before["trade_exists"]:
                if _backoff_active(uid, "trade"):
                    trade_result = "skipped_backoff"
                elif not _token_needs_refresh(before["trade_path"]):
                    trade_result = "skipped_not_due"
                    log.info("[AUTO-REFRESH] Trade token not due for refresh user_id=%s", uid)
                else:
                    log.info("[AUTO-REFRESH] Refreshing trade token for user_id=%s", uid)
                    trade_result = refresh_user_trade_token(uid)
                    if trade_result is None:
                        failures.append({"user_id": uid, "kind": "trade", "path": before["trade_path"]})
                        _set_backoff(uid, "trade", "refresh returned None")
                    else:
                        _clear_backoff(uid, "trade")

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

        ok = not failures
        if failures:
            log.error("[AUTO-REFRESH] Schwab token refresh failures: %s", failures)

        return {"ok": ok, "users": users, "data_dir": _data_dir(), "results": results, "failures": failures}

    except Exception as exc:
        log.exception("[AUTO-REFRESH] Failed refreshing Schwab tokens for users=%s", users)
        raise self.retry(exc=exc, countdown=60)
