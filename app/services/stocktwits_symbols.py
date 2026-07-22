from __future__ import annotations

import logging
import os
import re
import time
from html.parser import HTMLParser
from typing import Any

import requests


log = logging.getLogger(__name__)

STOCKTWITS_MOST_ACTIVE_API = "https://api.stocktwits.com/api/2/trending/most_active.json"
STOCKTWITS_MOST_ACTIVE_PAGE = "https://stocktwits.com/sentiment/most-active"
STOCKTWITS_MOST_ACTIVE_READER = "https://r.jina.ai/http://stocktwits.com/sentiment/most-active"
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_CACHE: dict[str, Any] = {"expires_at": 0.0, "symbols": []}


def stocktwits_most_active_symbols(limit: int | None = None) -> list[str]:
    """Return Stocktwits' current most-active equity symbols.

    Stocktwits' page is backed by a JSON endpoint but can also contain the
    ranked symbol links in its rendered HTML. Supporting both shapes gives us
    a graceful path through site changes; callers should still keep a local
    fallback because Stocktwits may rate-limit or challenge server traffic.
    """

    # Stocktwits exposes five ranked rows publicly and gates later rows behind
    # login. Operators may raise this if Stocktwits makes more rows public.
    resolved_limit = max(1, min(int(limit or os.getenv("SPARKIE_STOCKTWITS_LIMIT", "5")), 10))
    now = time.monotonic()
    cached = list(_CACHE.get("symbols") or [])
    if cached and float(_CACHE.get("expires_at") or 0.0) > now:
        return cached[:resolved_limit]

    timeout = max(1.0, min(float(os.getenv("SPARKIE_STOCKTWITS_TIMEOUT_SECONDS", "8")), 30.0))
    headers = {
        "Accept": "application/json, text/html;q=0.9",
        "Referer": STOCKTWITS_MOST_ACTIVE_PAGE,
        "User-Agent": "Mozilla/5.0 (compatible; Stockwicks-Sparkie/1.0)",
    }
    errors: list[str] = []
    symbols: list[str] = []

    try:
        response = requests.get(STOCKTWITS_MOST_ACTIVE_API, headers=headers, timeout=timeout)
        response.raise_for_status()
        symbols = _symbols_from_payload(response.json())
    except Exception as exc:
        errors.append(f"API: {exc}")

    if not symbols:
        try:
            response = requests.get(
                STOCKTWITS_MOST_ACTIVE_READER,
                headers={"Accept": "text/plain", "User-Agent": headers["User-Agent"]},
                timeout=timeout,
            )
            response.raise_for_status()
            symbols = _symbols_from_markdown(response.text)
        except Exception as exc:
            errors.append(f"reader: {exc}")

    if not symbols:
        try:
            response = requests.get(STOCKTWITS_MOST_ACTIVE_PAGE, headers=headers, timeout=timeout)
            response.raise_for_status()
            symbols = _symbols_from_html(response.text)
        except Exception as exc:
            errors.append(f"page: {exc}")

    symbols = _dedupe_symbols(symbols)[:resolved_limit]
    if not symbols:
        raise RuntimeError("Stocktwits Most Active was unavailable (" + "; ".join(errors) + ")")

    ttl = max(60, min(int(os.getenv("SPARKIE_STOCKTWITS_CACHE_SECONDS", "3600")), 86_400))
    _CACHE["symbols"] = symbols
    _CACHE["expires_at"] = now + ttl
    return symbols


def _symbols_from_payload(payload: Any) -> list[str]:
    """Extract ordered symbols while tolerating Stocktwits response wrappers."""

    if isinstance(payload, dict):
        for key in ("symbols", "data", "results", "stocks"):
            value = payload.get(key)
            extracted = _symbols_from_payload(value)
            if extracted:
                return extracted
        symbol = payload.get("symbol") or payload.get("ticker")
        return [str(symbol)] if symbol else []
    if isinstance(payload, list):
        symbols: list[str] = []
        for value in payload:
            if isinstance(value, str):
                symbols.append(value)
            elif isinstance(value, dict):
                symbol = value.get("symbol") or value.get("ticker")
                if symbol:
                    symbols.append(str(symbol))
                else:
                    symbols.extend(_symbols_from_payload(value))
        return symbols
    return []


def _symbols_from_html(html: str) -> list[str]:
    parser = _MostActiveHtmlParser()
    parser.feed(html or "")
    return parser.symbols


def _symbols_from_markdown(markdown: str) -> list[str]:
    """Extract only numbered rows from the rendered Most Active table."""

    text = (markdown or "").replace("\r\n", "\n")
    heading_at = text.find("# Most Active")
    if heading_at < 0:
        return []
    table_at = text.find("\nRank\n", heading_at)
    if table_at < 0:
        return []
    gated_at = text.find("Join the conversation", table_at)
    table = text[table_at : gated_at if gated_at >= 0 else len(text)]
    matches = re.findall(
        r"(?:^|\n)(\d+)\n\n\[([A-Za-z][A-Za-z0-9.-]{0,9})\]\(https?://stocktwits\.com/symbol/[^)]+\)",
        table,
    )
    ranked: list[tuple[int, str]] = []
    for rank_text, symbol in matches:
        rank = int(rank_text)
        if rank > 0 and all(existing_rank != rank for existing_rank, _ in ranked):
            ranked.append((rank, symbol))
    return [symbol for _, symbol in sorted(ranked)]


class _MostActiveHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.table_depth: int | None = None
        self.symbols: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = dict(attrs)
        class_name = attrs_by_name.get("class") or ""
        if self.table_depth is None and "Rankings_tickerTableNewColumns" in class_name:
            self.table_depth = self.depth
        if self.table_depth is not None and tag.lower() == "a":
            href = attrs_by_name.get("href") or ""
            match = re.fullmatch(r"(?:https://stocktwits\.com)?/symbol/([A-Za-z][A-Za-z0-9.-]{0,9})", href)
            if match:
                self.symbols.append(match.group(1))
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.depth = max(0, self.depth - 1)
        if self.table_depth is not None and self.depth <= self.table_depth:
            self.table_depth = None


def _dedupe_symbols(values: list[str]) -> list[str]:
    symbols: list[str] = []
    for value in values:
        symbol = str(value or "").upper().strip()
        # Stocktwits represents crypto streams with a .X suffix. Sparkie's
        # market-data/backtest path is equities-only, so do not enqueue them.
        if symbol.endswith(".X"):
            continue
        if _SYMBOL_PATTERN.fullmatch(symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols
