#!/usr/bin/env python3
"""
Extract AlgoMM OPEN/CLOSE LONG/SHORT blocks from bot logs into CSV.

Key fix vs v1:
- Adds executed=True/False
- Adds --orders-only to keep only executed orders:
    * OPEN_LONG executed if REASON == LONG_CONDITIONS_MET
    * OPEN_SHORT executed if REASON == SHORT_CONDITIONS_MET
    * CLOSE_* executed if action is CLOSE_LONG/CLOSE_SHORT (if such blocks exist)
- Adds cleaner summaries: all blocks vs executed orders

Default log dir:
  /var/www/stockwicks/data/116
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional


ACTIONS = {"OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "CLOSE_SHORT"}

# Box header line example:
# [2025-12-08 09:31:32 EST] OPEN_LONG
HEADER_RE = re.compile(
    r"\[(?P<header_ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(?P<header_tz>[A-Z]{2,5})\]\s+"
    r"(?P<header_action>OPEN_LONG|CLOSE_LONG|OPEN_SHORT|CLOSE_SHORT)"
)

MODEL_RE = re.compile(r"Model name:\s*(?P<model>.+)")
SYMBOL_RE = re.compile(r"Symbol:\s*(?P<symbol>[A-Z0-9\.\-_]+)")
TIME_RE = re.compile(r"Time:\s*(?P<time>.+)")
PROBS_RE = re.compile(r"UP:\s*(?P<up>[0-9]*\.[0-9]+)\s*\|\s*DOWN:\s*(?P<down>[0-9]*\.[0-9]+)")
THRESH_RE = re.compile(
    r"Thresholds:\s*LONG:\s*(?P<long_a>[0-9]*\.[0-9]+)\/(?P<long_b>[0-9]*\.[0-9]+)\s*\|\s*SHORT:\s*(?P<short_a>[0-9]*\.[0-9]+)\/(?P<short_b>[0-9]*\.[0-9]+)"
)
PRICE_USED_RE = re.compile(
    r"Price Used:\s*OPEN:\s*\$\s*(?P<popen>[0-9\.,]+)\s*\|\s*CLOSE:\s*\$\s*(?P<pclose>[0-9\.,]+)"
)
POSITION_RE = re.compile(
    r"Position:\s*(?P<side>LONG|SHORT)\s*@\s*\$(?P<price>[0-9\.,]+)\s*P&L:\s*\$(?P<pnl>[-0-9\.,]+)"
)
ACTION_RE = re.compile(r"ACTION:\s*(?P<action>OPEN_LONG|CLOSE_LONG|OPEN_SHORT|CLOSE_SHORT)")
REASON_RE = re.compile(r"REASON:\s*(?P<reason>.+)")


def _to_float(x: Optional[str]) -> Optional[float]:
    if x is None:
        return None
    x = x.strip().replace(",", "")
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _clean(x: Optional[str]) -> Optional[str]:
    if x is None:
        return None
    x = x.strip()
    return x or None


def _is_executed(action: Optional[str], reason: Optional[str]) -> bool:
    """
    Define what "executed order" means from these logs.
    Adjust here if your system uses different reason strings.
    """
    if not action:
        return False

    if action == "OPEN_LONG":
        return (reason or "").strip() == "LONG_CONDITIONS_MET"
    if action == "OPEN_SHORT":
        return (reason or "").strip() == "SHORT_CONDITIONS_MET"
    if action in ("CLOSE_LONG", "CLOSE_SHORT"):
        return True

    return False


@dataclass
class EventRow:
    file: str
    bot_id: Optional[str]
    line_no: int

    header_ts: Optional[str]
    header_tz: Optional[str]
    header_action: Optional[str]

    model: Optional[str] = None
    symbol: Optional[str] = None
    time_line: Optional[str] = None

    prob_up: Optional[float] = None
    prob_down: Optional[float] = None

    thresh_long_a: Optional[float] = None
    thresh_long_b: Optional[float] = None
    thresh_short_a: Optional[float] = None
    thresh_short_b: Optional[float] = None

    price_used_open: Optional[float] = None
    price_used_close: Optional[float] = None

    position_side: Optional[str] = None
    position_price: Optional[float] = None
    pnl: Optional[float] = None

    action: Optional[str] = None
    reason: Optional[str] = None

    executed: bool = False


def parse_file(path: str) -> List[EventRow]:
    rows: List[EventRow] = []
    base = os.path.basename(path)

    bot_id = None
    m = re.search(r"bot_(\d+)_", base)
    if m:
        bot_id = m.group(1)

    current: Optional[EventRow] = None

    def flush():
        nonlocal current
        if current is None:
            return

        # prefer ACTION: line if present, else header_action
        if current.action is None:
            current.action = current.header_action

        # compute executed
        current.executed = _is_executed(current.action, current.reason)

        # keep only recognized actions
        if (current.action in ACTIONS) or (current.header_action in ACTIONS):
            rows.append(current)

        current = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            h = HEADER_RE.search(line)
            if h:
                flush()
                current = EventRow(
                    file=base,
                    bot_id=bot_id,
                    line_no=i,
                    header_ts=h.group("header_ts"),
                    header_tz=h.group("header_tz"),
                    header_action=h.group("header_action"),
                )
                continue

            if current is None:
                continue

            if (m := MODEL_RE.search(line)):
                current.model = _clean(m.group("model"))
            elif (m := SYMBOL_RE.search(line)):
                current.symbol = _clean(m.group("symbol"))
            elif (m := TIME_RE.search(line)):
                current.time_line = _clean(m.group("time"))
            elif (m := PROBS_RE.search(line)):
                current.prob_up = _to_float(m.group("up"))
                current.prob_down = _to_float(m.group("down"))
            elif (m := THRESH_RE.search(line)):
                current.thresh_long_a = _to_float(m.group("long_a"))
                current.thresh_long_b = _to_float(m.group("long_b"))
                current.thresh_short_a = _to_float(m.group("short_a"))
                current.thresh_short_b = _to_float(m.group("short_b"))
            elif (m := PRICE_USED_RE.search(line)):
                current.price_used_open = _to_float(m.group("popen"))
                current.price_used_close = _to_float(m.group("pclose"))
            elif (m := POSITION_RE.search(line)):
                current.position_side = _clean(m.group("side"))
                current.position_price = _to_float(m.group("price"))
                current.pnl = _to_float(m.group("pnl"))
            elif (m := ACTION_RE.search(line)):
                current.action = _clean(m.group("action"))
            elif (m := REASON_RE.search(line)):
                current.reason = _clean(m.group("reason"))

    flush()
    return rows


def summarize(rows: List[EventRow], title: str) -> None:
    counts: Dict[str, int] = {a: 0 for a in sorted(ACTIONS)}
    for r in rows:
        a = r.action or r.header_action or "UNKNOWN"
        if a in counts:
            counts[a] += 1

    print(f"\n=== Summary ({title}) ===")
    for a in sorted(ACTIONS):
        print(f"{a:11s}  count={counts[a]:4d}")
    print(f"Total rows: {len(rows)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract AlgoMM OPEN/CLOSE LONG/SHORT blocks from bot logs.")
    ap.add_argument(
        "--log-dir",
        default="/var/www/stockwicks/data/116",
        help="Directory where bot log files live (default: /var/www/stockwicks/data/116)",
    )
    ap.add_argument("--glob", required=True, help='Glob like "bot_*_TSLA_AlgoMM.log"')
    ap.add_argument("--out", required=True, help="Output CSV path for ALL parsed blocks")
    ap.add_argument("--orders-out", default=None, help="Optional CSV path for executed orders only")
    ap.add_argument("--orders-only", action="store_true", help="Only write executed orders to --out")
    ap.add_argument("--summary", action="store_true", help="Print summaries")

    args = ap.parse_args()

    pattern = os.path.join(args.log_dir, args.glob)
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"No files matched: {pattern}")

    all_rows: List[EventRow] = []
    for p in files:
        all_rows.extend(parse_file(p))

    executed_rows = [r for r in all_rows if r.executed]

    # choose what goes to --out
    out_rows = executed_rows if args.orders_only else all_rows

    # fieldnames
    fieldnames = list(asdict(out_rows[0]).keys()) if out_rows else list(EventRow.__dataclass_fields__.keys())

    # ensure output dir exists
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", newline="", encoding="utf-8") as out:
        w = csv.DictWriter(out, fieldnames=fieldnames)
        w.writeheader()
        for r in out_rows:
            w.writerow(asdict(r))

    print(f"Wrote {len(out_rows)} rows -> {args.out}")

    # optional separate executed-orders CSV
    if args.orders_out:
        orders_dir = os.path.dirname(os.path.abspath(args.orders_out))
        if orders_dir and not os.path.exists(orders_dir):
            os.makedirs(orders_dir, exist_ok=True)
        orders_fieldnames = list(asdict(executed_rows[0]).keys()) if executed_rows else fieldnames
        with open(args.orders_out, "w", newline="", encoding="utf-8") as out2:
            w2 = csv.DictWriter(out2, fieldnames=orders_fieldnames)
            w2.writeheader()
            for r in executed_rows:
                w2.writerow(asdict(r))
        print(f"Wrote {len(executed_rows)} executed-order rows -> {args.orders_out}")

    if args.summary:
        summarize(all_rows, "ALL blocks")
        summarize(executed_rows, "EXECUTED orders only")


if __name__ == "__main__":
    main()
