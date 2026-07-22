from __future__ import annotations

import csv
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests


log = logging.getLogger(__name__)

BARCHART_TOP_100_PAGE = (
    "https://www.barchart.com/stocks/top-100-stocks"
    "?orderBy=weightedAlpha&orderDir=desc"
)
BARCHART_TOP_100_API = "https://www.barchart.com/proxies/core-api/v1/quotes/get"
BARCHART_TOP_100_FIELDS = (
    "symbol",
    "symbolName",
    "weightedAlpha",
    "currentRankUsTop100",
    "previousRank",
    "lastPrice",
    "priceChange",
    "percentChange",
    "highPrice1y",
    "lowPrice1y",
    "percentChange1y",
    "tradeTime",
    "symbolCode",
    "hasOptions",
    "symbolType",
)
BARCHART_CSV_FIELDS = BARCHART_TOP_100_FIELDS[:12]
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_CACHE: dict[str, Any] = {"rows": [], "expires_at": 0.0}
_SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "data" / "barchart_top_100_stocks.csv"


def barchart_top_symbols(limit: int = 5) -> list[str]:
    return barchart_top_symbols_with_source(limit)[0]


def barchart_top_symbols_with_source(limit: int = 5) -> tuple[list[str], str]:
    resolved_limit = max(1, min(int(limit), 100))
    try:
        rows = barchart_top_100_rows()
        source = "live"
    except Exception as exc:
        log.warning("[SPARKIE] Barchart live ranking unavailable; using CSV snapshot: %s", exc)
        rows = _read_snapshot_rows()
        source = "snapshot"
    symbols = [str(row["symbol"]) for row in rows[:resolved_limit]]
    if not symbols:
        raise RuntimeError("Barchart Top 100 and its CSV snapshot were unavailable.")
    return symbols, source


def barchart_top_100_rows(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return Barchart's public Top 100 table ordered by Weighted Alpha."""

    now = time.monotonic()
    cached = list(_CACHE.get("rows") or [])
    if cached and not force_refresh and float(_CACHE.get("expires_at") or 0.0) > now:
        return cached

    timeout = max(2.0, min(float(os.getenv("SPARKIE_BARCHART_TIMEOUT_SECONDS", "12")), 30.0))
    user_agent = "Mozilla/5.0 (compatible; Stockwicks-Sparkie/1.0)"
    session = requests.Session()
    page_response = session.get(
        BARCHART_TOP_100_PAGE,
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": user_agent},
        timeout=timeout,
    )
    page_response.raise_for_status()

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": BARCHART_TOP_100_PAGE,
        "User-Agent": user_agent,
        "X-Requested-With": "XMLHttpRequest",
    }
    xsrf_token = session.cookies.get("XSRF-TOKEN")
    if xsrf_token:
        headers["X-XSRF-TOKEN"] = unquote(xsrf_token)

    response = session.get(
        BARCHART_TOP_100_API,
        params={
            "list": "stocks.us.weighted_alpha.advances",
            "fields": ",".join(BARCHART_TOP_100_FIELDS),
            "orderBy": "weightedAlpha",
            "orderDir": "desc",
            "meta": "field.shortName,field.type,field.description,lists.lastUpdate",
            "page": 1,
            "limit": 100,
            "hasOptions": "true",
            "raw": 1,
        },
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list):
        raise RuntimeError("Barchart Top 100 returned an unexpected response.")

    rows: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            continue
        symbol = str(raw_row.get("symbol") or "").upper().strip()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            continue
        row = {field: raw_row.get(field) for field in BARCHART_TOP_100_FIELDS}
        row["symbol"] = symbol
        rows.append(row)

    rows.sort(key=lambda row: _rank_value(row.get("currentRankUsTop100")))
    if len(rows) < 5:
        raise RuntimeError(f"Barchart Top 100 returned only {len(rows)} valid equities.")

    ttl = max(60, min(int(os.getenv("SPARKIE_BARCHART_CACHE_SECONDS", "600")), 86_400))
    _CACHE.update({"rows": rows, "expires_at": now + ttl})
    return rows


def write_barchart_top_100_csv(path: str | Path, rows: list[dict[str, Any]] | None = None) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected_rows = rows if rows is not None else barchart_top_100_rows()
    with destination.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=BARCHART_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(selected_rows)
    return destination


def _rank_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1_000_000


def _read_snapshot_rows() -> list[dict[str, Any]]:
    if not _SNAPSHOT_PATH.exists():
        return []
    with _SNAPSHOT_PATH.open("r", newline="", encoding="utf-8-sig") as csv_file:
        rows = [dict(row) for row in csv.DictReader(csv_file)]
    rows = [row for row in rows if _SYMBOL_PATTERN.fullmatch(str(row.get("symbol") or ""))]
    rows.sort(key=lambda row: _rank_value(row.get("currentRankUsTop100")))
    return rows
