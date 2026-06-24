from __future__ import annotations

import os
from pathlib import Path


def client_root() -> Path:
    raw = os.getenv("CLIENT_ROOT", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2]


def client_slug() -> str:
    return (os.getenv("CLIENT_SLUG", "").strip() or client_root().name or "client")


def data_dir() -> Path:
    raw = os.getenv("DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    return client_root() / "data"


def public_prefix() -> str:
    raw = os.getenv("CLIENT_PUBLIC_PREFIX", "").strip().rstrip("/")
    if raw:
        return raw
    return f"/clients/{client_slug()}"


def public_base_url() -> str:
    raw = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("CLIENT_PUBLIC_BASE_URL")
        or os.getenv("APP_PUBLIC_URL")
        or ""
    ).strip().rstrip("/")
    if raw:
        return raw
    return f"https://www.stockwicks.com{public_prefix()}"
