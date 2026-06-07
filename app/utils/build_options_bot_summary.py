#!/usr/bin/env python3
"""
build_options_bot_summary.py

Parse today's option bot logs (bot_*_OPTIONS.log) and extract ManageTrades close events.

Supports winners and losers (negative gross/net).

Outputs:
  - bot_summary_OPTIONS_YYYY-MM-DD.csv
  - bot_summary_OPTIONS_YYYY-MM-DD.txt
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, date
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore


TS_FMT = "%Y-%m-%d %H:%M:%S,%f"

# allow negative numbers for mark/gross/net/fees/entry
NUM = r"-?[0-9]+(?:\.[0-9]+)?"

RE_MARK = re.compile(
    rf"^(?P<ts>\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}},\d{{3}})\s+\|\s+INFO\s+\|\s+\[ManageTrades\]\s+"
    rf"Trade\s+(?P<trade_id>\d+):\s+mark=\$(?P<mark>{NUM})\s+"
    rf"gross=\$(?P<gross>{NUM})\s+net=\$(?P<net>{NUM})\s+"
    rf"\(fees=\$(?P<fees>{NUM}),\s+qty=(?P<qty>[0-9]+(?:\.[0-9]+)?),\s+"
    rf"entry=\$(?P<entry>{NUM}),\s+side=(?P<side>[a-zA-Z]+)\)"
)

RE_SHOULD_CLOSE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+\|\s+INFO\s+\|\s+\[ManageTrades\]\s+"
    r"Trade\s+(?P<trade_id>\d+):\s+ShouldClose=True\s+reason=(?P<reason_code>[A-Z0-9_]+)\s*(?P<reason_tail>\(.*\))?$"
)

RE_CLOSED = re.compile(
    rf"^(?P<ts>\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}},\d{{3}})\s+\|\s+INFO\s+\|\s+\[ManageTrades\]\s+"
    rf"Trade\s+(?P<trade_id>\d+):\s+✅\s+closed\s+hist_id=(?P<hist_id>\d+)\s+"
    rf"\(gross=\$(?P<gross>{NUM}),\s+net=\$(?P<net>{NUM})\)\s+"
    rf"reason=(?P<reason_full>.+)$"
)

RE_BOT_ID = re.compile(r"bot_(?P<bot_id>\d+)_OPTIONS\.log$")


@dataclass
class TradeSummary:
    date: str
    bot_id: str
    log_file: str
    trade_id: str
    hist_id: str

    # timestamps
    ts_mark: str
    ts_should_close: str
    ts_closed: str

    # trade numbers
    side: str
    qty: float
    entry: float
    exit_mark: float
    fees: float
    pnl_gross: float
    pnl_net: float

    # reason
    reason_code: str
    reason_full: str


def parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts_str, TS_FMT)
    except Exception:
        return None


def file_is_for_date(filepath: str, target_date: date, tz_name: str) -> bool:
    """Filter by file modified time (mtime) to match 'today bots only' list."""
    try:
        st = os.stat(filepath)
    except FileNotFoundError:
        return False

    dt = datetime.fromtimestamp(st.st_mtime)
    if ZoneInfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return dt.date() == target_date


def safe_float(x: str, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def parse_log_file(filepath: str, target_date_str: str) -> List[TradeSummary]:
    """Parse one bot_*_OPTIONS.log and return finalized closed trade records."""
    bot_id = "unknown"
    m = RE_BOT_ID.search(os.path.basename(filepath))
    if m:
        bot_id = m.group("bot_id")

    state: Dict[str, Dict[str, object]] = {}
    results: List[TradeSummary] = []

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")

            m1 = RE_MARK.match(line)
            if m1:
                tid = m1.group("trade_id")
                st = state.setdefault(tid, {})
                st.update({
                    "ts_mark": m1.group("ts"),
                    "entry": safe_float(m1.group("entry")),
                    "exit_mark": safe_float(m1.group("mark")),
                    "qty": safe_float(m1.group("qty")),
                    "side": m1.group("side").lower(),
                    "fees": safe_float(m1.group("fees")),
                    "pnl_gross": safe_float(m1.group("gross")),
                    "pnl_net": safe_float(m1.group("net")),
                })
                continue

            m2 = RE_SHOULD_CLOSE.match(line)
            if m2:
                tid = m2.group("trade_id")
                st = state.setdefault(tid, {})
                st.update({
                    "ts_should_close": m2.group("ts"),
                    "reason_code": m2.group("reason_code"),
                    "reason_tail": (m2.group("reason_tail") or "").strip(),
                })
                continue

            m3 = RE_CLOSED.match(line)
            if m3:
                tid = m3.group("trade_id")
                st = state.setdefault(tid, {})

                ts_closed = m3.group("ts")
                hist_id = m3.group("hist_id")

                pnl_gross = safe_float(m3.group("gross"))
                pnl_net = safe_float(m3.group("net"))
                reason_full = m3.group("reason_full").strip()

                reason_code = str(st.get("reason_code") or "").strip()
                if not reason_code:
                    reason_code = reason_full.split(" ", 1)[0].split("(", 1)[0].strip()

                entry = float(st.get("entry") or 0.0)
                exit_mark = float(st.get("exit_mark") or 0.0)
                qty = float(st.get("qty") or 0.0)
                fees = float(st.get("fees") or 0.0)
                side = str(st.get("side") or "").lower()

                ts_mark = str(st.get("ts_mark") or "")
                ts_should = str(st.get("ts_should_close") or "")

                results.append(TradeSummary(
                    date=target_date_str,
                    bot_id=str(bot_id),
                    log_file=os.path.basename(filepath),
                    trade_id=str(tid),
                    hist_id=str(hist_id),

                    ts_mark=ts_mark,
                    ts_should_close=ts_should,
                    ts_closed=ts_closed,

                    side=side,
                    qty=qty,
                    entry=entry,
                    exit_mark=exit_mark,
                    fees=fees,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,

                    reason_code=reason_code,
                    reason_full=reason_full,
                ))
                continue

    results.sort(key=lambda r: parse_ts(r.ts_closed) or datetime.min)
    return results


def format_table(rows: List[TradeSummary]) -> str:
    headers = [
        "Closed TS", "Bot", "Trade", "Hist", "Side", "Qty",
        "Entry", "Exit(mark)", "PnL Gross", "PnL Net", "Fees", "Reason"
    ]

    data: List[List[str]] = []
    for r in rows:
        data.append([
            r.ts_closed,
            r.bot_id,
            r.trade_id,
            r.hist_id,
            r.side,
            f"{r.qty:.2f}".rstrip("0").rstrip("."),
            f"{r.entry:.4f}".rstrip("0").rstrip("."),
            f"{r.exit_mark:.4f}".rstrip("0").rstrip("."),
            f"{r.pnl_gross:.2f}",
            f"{r.pnl_net:.2f}",
            f"{r.fees:.2f}",
            r.reason_code,
        ])

    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(row: List[str]) -> str:
        return " | ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers)))

    sep = "-+-".join("-" * w for w in widths)

    out = [fmt_row(headers), sep]
    out.extend(fmt_row(row) for row in data)
    return "\n".join(out)


def write_csv(path: str, rows: List[TradeSummary]) -> None:
    import csv
    if not rows:
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        return

    fieldnames = list(asdict(rows[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/var/www/stockwicks/data/116",
                    help="Folder containing bot_*_OPTIONS.log")
    ap.add_argument("--date", default=None,
                    help="Target date YYYY-MM-DD (default: today in tz)")
    ap.add_argument("--tz", default="America/Chicago",
                    help="Timezone used for 'today' file filtering")
    ap.add_argument("--output-prefix", default="bot_summary_OPTIONS",
                    help="Output file prefix")
    args = ap.parse_args()

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except Exception:
            print("ERROR: --date must be YYYY-MM-DD", file=sys.stderr)
            return 2
    else:
        now = datetime.now()
        if ZoneInfo is not None:
            try:
                now = now.astimezone(ZoneInfo(args.tz))
            except Exception:
                pass
        target_date = now.date()

    target_date_str = target_date.isoformat()

    if not os.path.isdir(args.data_dir):
        print(f"ERROR: data dir not found: {args.data_dir}", file=sys.stderr)
        return 2

    # find today's option logs (mtime filter)
    option_logs: List[str] = []
    for name in os.listdir(args.data_dir):
        if not name.startswith("bot_") or not name.endswith("_OPTIONS.log"):
            continue
        fp = os.path.join(args.data_dir, name)
        if file_is_for_date(fp, target_date, args.tz):
            option_logs.append(fp)

    option_logs.sort()

    all_rows: List[TradeSummary] = []
    for fp in option_logs:
        all_rows.extend(parse_log_file(fp, target_date_str))

    out_csv = os.path.join(args.data_dir, f"{args.output_prefix}_{target_date_str}.csv")
    out_txt = os.path.join(args.data_dir, f"{args.output_prefix}_{target_date_str}.txt")

    write_csv(out_csv, all_rows)

    if all_rows:
        table = format_table(all_rows)
    else:
        table = f"No closed option trades found for {target_date_str} in today's bot_*_OPTIONS.log files."

    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(table + "\n")

    print(f"=== Options Bot Summary ({target_date_str}) ===")
    print(f"Logs scanned: {len(option_logs)}")
    print(f"Rows: {len(all_rows)}")
    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_txt}\n")
    print(table)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
