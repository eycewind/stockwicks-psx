from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("schwab_circuit")

SCHWAB_HOST = "api.schwabapi.com"


class SchwabCircuitOpen(RuntimeError):
    """Raised when local Schwab cooldown is active."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_enabled(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _client_root() -> str:
    return os.getenv("CLIENT_ROOT") or str(Path(__file__).resolve().parents[2])


def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", f"{_client_root()}/data"))


def _state_path() -> Path:
    return Path(os.getenv("SCHWAB_CIRCUIT_FILE", str(_data_dir() / ".schwab_api_circuit.json")))


def _window_seconds() -> int:
    return _env_int("SCHWAB_ERROR_WINDOW_SECONDS", 60)


def _threshold() -> int:
    return _env_int("SCHWAB_ERROR_THRESHOLD", 3)


def _cooldown_seconds() -> int:
    # Schwab says wait at least 10 minutes. Default to 11 for a small buffer.
    return _env_int("SCHWAB_CIRCUIT_COOLDOWN_SECONDS", 11 * 60)


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(path)
    except Exception:
        log.warning("[SCHWAB CIRCUIT] Could not save circuit state", exc_info=True)


def _is_schwab_url(url: str) -> bool:
    return SCHWAB_HOST in str(url).lower()


def _cooldown_remaining(state: dict[str, Any] | None = None) -> int:
    state = state if state is not None else _load_state()
    until = float(state.get("cooldown_until_epoch") or 0)
    return max(0, int(until - time.time()))


def cooldown_remaining_seconds() -> int:
    return _cooldown_remaining()


def clear_schwab_circuit() -> None:
    _save_state({"errors": [], "cooldown_until_epoch": 0, "cleared_epoch": int(time.time())})


def _assert_allowed(url: str) -> None:
    if not _env_enabled("SCHWAB_CIRCUIT_ENABLED"):
        return
    if not _is_schwab_url(url):
        return

    state = _load_state()
    remaining = _cooldown_remaining(state)
    if remaining > 0:
        raise SchwabCircuitOpen(
            f"Local Schwab cooldown active for {remaining}s after recent non-2xx responses."
        )


def _record_success(url: str) -> None:
    if not _env_enabled("SCHWAB_CIRCUIT_ENABLED") or not _is_schwab_url(url):
        return
    state = _load_state()
    now = time.time()
    window_start = now - _window_seconds()
    errors = [e for e in state.get("errors", []) if float(e.get("epoch", 0)) >= window_start]
    state["errors"] = errors
    if not errors and state.get("cooldown_until_epoch"):
        state["cooldown_until_epoch"] = 0
    _save_state(state)


def _record_error(url: str, status_code: int | str, preview: str = "") -> None:
    if not _env_enabled("SCHWAB_CIRCUIT_ENABLED") or not _is_schwab_url(url):
        return

    now = time.time()
    state = _load_state()
    window_start = now - _window_seconds()
    errors = [
        e
        for e in state.get("errors", [])
        if float(e.get("epoch", 0)) >= window_start
    ]
    errors.append(
        {
            "epoch": int(now),
            "status_code": status_code,
            "url": str(url).split("?")[0],
            "preview": preview[:200],
        }
    )
    state["errors"] = errors
    state["last_error_epoch"] = int(now)
    state["last_error_status"] = status_code

    if len(errors) >= _threshold():
        state["cooldown_until_epoch"] = int(now + _cooldown_seconds())
        state["cooldown_reason"] = (
            f"{len(errors)} Schwab errors in {_window_seconds()}s; "
            f"cooling down for {_cooldown_seconds()}s"
        )
        log.error("[SCHWAB CIRCUIT] %s", state["cooldown_reason"])

    _save_state(state)


def schwab_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    _assert_allowed(url)
    try:
        resp = requests.request(method, url, **kwargs)
    except Exception as exc:
        _record_error(url, "exception", str(exc))
        raise

    if 200 <= int(resp.status_code) < 300:
        _record_success(url)
    else:
        _record_error(url, resp.status_code, getattr(resp, "text", "")[:200])
    return resp


def schwab_get(url: str, **kwargs: Any) -> requests.Response:
    return schwab_request("GET", url, **kwargs)


def schwab_post(url: str, **kwargs: Any) -> requests.Response:
    return schwab_request("POST", url, **kwargs)


def schwab_put(url: str, **kwargs: Any) -> requests.Response:
    return schwab_request("PUT", url, **kwargs)


def schwab_delete(url: str, **kwargs: Any) -> requests.Response:
    return schwab_request("DELETE", url, **kwargs)


class SchwabGuardedSession(requests.Session):
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        _assert_allowed(url)
        try:
            resp = super().request(method, url, **kwargs)
        except Exception as exc:
            _record_error(url, "exception", str(exc))
            raise
        if 200 <= int(resp.status_code) < 300:
            _record_success(url)
        else:
            _record_error(url, resp.status_code, getattr(resp, "text", "")[:200])
        return resp


def schwab_session() -> requests.Session:
    session = SchwabGuardedSession()
    session.trust_env = os.getenv("SCHWAB_TRUST_ENV_PROXIES", "0").strip().lower() in {"1", "true", "yes"}
    return session
