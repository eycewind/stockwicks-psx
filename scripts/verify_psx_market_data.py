#!/usr/bin/env python3
"""Bounded read-only C3 smoke check with source fingerprint verification."""

from __future__ import annotations

import argparse
import hashlib
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.market_data.history import fetch_compatibility_response
from app.market_data.psx_sqlite import trade_date_to_epoch_ms


def fingerprint(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--start", default="2023-02-23", type=date.fromisoformat)
    parser.add_argument("--end", default="2023-03-03", type=date.fromisoformat)
    parser.add_argument("--symbols", nargs="+", default=["DGKC", "OGDC"])
    args = parser.parse_args()

    db_path = args.db.resolve()
    os.environ.update(
        {
            "MARKET_DATA_PROVIDER": "psx_sqlite",
            "PSX_DB_PATH": str(db_path),
            "PSX_PRICE_MODE": "adjusted",
        }
    )
    before = fingerprint(db_path)
    print("fingerprint_before", before)

    for symbol in args.symbols:
        response = fetch_compatibility_response(
            {
                "symbol": symbol,
                "frequencyType": "daily",
                "frequency": 1,
                "startDate": trade_date_to_epoch_ms(args.start),
                "endDate": trade_date_to_epoch_ms(args.end),
                "needExtendedHoursData": "false",
                "needPreviousClose": "false",
            }
        )
        dates = [
            datetime.fromtimestamp(
                candle["datetime"] / 1000, ZoneInfo("Asia/Karachi")
            ).date()
            for candle in response["candles"]
        ]
        if not dates or dates != sorted(dates):
            raise RuntimeError(f"{symbol}: missing or unordered candles")
        if dates[0] < args.start or dates[-1] > args.end:
            raise RuntimeError(f"{symbol}: returned candle outside requested bounds")
        print(
            symbol,
            {
                "bar_count": len(response["candles"]),
                "first_date": dates[0].isoformat(),
                "last_date": dates[-1].isoformat(),
                "quality": response["quality"],
            },
        )

    after = fingerprint(db_path)
    print("fingerprint_after", after)
    print("fingerprint_unchanged", before == after)
    if before != after:
        raise RuntimeError("PSX database fingerprint changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
