#!/usr/bin/env python3
"""
analyze_logs.py — Entry/Exit-only AlgoMM log extractor

Goal:
  Provide only bot ID and print only real trade logs:
    - OPEN / ENTER blocks
    - EXIT / CLOSE blocks

Skips:
  - NO_ENTRY_SIGNAL
  - NO_ENTRY
  - NO_ACTION
  - HOLD
  - COOLDOWN
  - WARMUP_NOT_READY
  - STALE_BAR
  - any other non-trade action

Usage:
  python analyze_logs.py 709
  python analyze_logs.py 709 --raw
  python analyze_logs.py /var/www/stockwicks/data/116/bot_709_ARM_AlgoMM.log
  python analyze_logs.py 709 --data-root /var/www/stockwicks/data
"""

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional


TRADE_ENTRY_WORDS = (
    "OPEN_LONG",
    "OPEN_SHORT",
    "ENTER_LONG",
    "ENTER_SHORT",
    "BUY",
    "SELL_SHORT",
)

TRADE_EXIT_WORDS = (
    "EXIT",
    "CLOSE",
    "CLOSE_LONG",
    "CLOSE_SHORT",
)

SKIP_ACTION_WORDS = (
    "NO_ENTRY",
    "NO_ENTRY_SIGNAL",
    "NO_ACTION",
    "HOLD",
    "COOLDOWN",
    "WARMUP",
    "WARMUP_NOT_READY",
    "STALE_BAR",
    "FLAT",
)


@dataclass
class TradeEvent:
    event_type: str
    action: str
    time: str
    symbol: str
    price_open: Optional[float]
    price_close: Optional[float]
    price: Optional[float]
    prob_up: Optional[float]
    prob_down: Optional[float]
    reason: str
    position: str
    features: str
    raw_block: str


def money_to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value.replace(",", "").strip())
    except Exception:
        return None


def find_log_by_bot_id(bot_id: str, data_root: str) -> str:
    pattern = os.path.join(data_root, "*", f"bot_{bot_id}_*_AlgoMM.log")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    if not matches:
        print(f"No AlgoMM log found for bot_id={bot_id}")
        print(f"Searched: {pattern}")
        sys.exit(1)

    if len(matches) > 1:
        print(f"Found multiple logs for bot_id={bot_id}; using newest:")
        for i, path in enumerate(matches[:5], 1):
            marker = " <-- selected" if i == 1 else ""
            print(f"  {i}. {path}{marker}")
        print()

    return matches[0]


def split_blocks(text: str) -> List[str]:
    """
    Handles box-style logs like:

    ╔════
    ...
    ╚════

    Also has fallback for plain logs.
    """
    if "╔" in text and "╚" in text:
        blocks = re.findall(r"╔.*?╚[═=]+", text, flags=re.DOTALL)
        if blocks:
            return blocks

    if "╔" in text:
        raw = text.split("╔")
        return ["╔" + b for b in raw if b.strip()]

    if "=" * 20 in text:
        parts = re.split(r"\n={20,}\n", text)
        return [p for p in parts if p.strip()]

    return [b for b in text.split("\n\n") if b.strip()]


def extract_first(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else default


def extract_probabilities(block: str):
    """
    Supports both:
      Probabilities:  UP: 0.526 | DOWN: 0.474
      Prob UP: 0.526 | Prob DOWN: 0.474
    """
    patterns = [
        r"Probabilities:\s*UP:\s*([0-9.]+)\s*\|\s*DOWN:\s*([0-9.]+)",
        r"Prob\s*UP:\s*([0-9.]+)\s*\|\s*Prob\s*DOWN:\s*([0-9.]+)",
        r"UP:\s*([0-9.]+)\s*\|\s*DOWN:\s*([0-9.]+)",
    ]

    for pat in patterns:
        m = re.search(pat, block, flags=re.IGNORECASE)
        if m:
            return float(m.group(1)), float(m.group(2))

    return None, None


def extract_prices(block: str):
    """
    Supports:
      Price Used:    OPEN: $ 232.00 | CLOSE: $ 227.47
      Price: $227.47
    """
    open_price = None
    close_price = None
    price = None

    m = re.search(
        r"Price Used:\s*OPEN:\s*\$?\s*([0-9.,]+)\s*\|\s*CLOSE:\s*\$?\s*([0-9.,]+)",
        block,
        flags=re.IGNORECASE,
    )
    if m:
        open_price = money_to_float(m.group(1))
        close_price = money_to_float(m.group(2))

    m = re.search(r"\bPrice:\s*\$?\s*([0-9.,]+)", block, flags=re.IGNORECASE)
    if m:
        price = money_to_float(m.group(1))

    # Prefer close price for decision/event price if available.
    if price is None:
        price = close_price if close_price is not None else open_price

    return open_price, close_price, price


def classify_action(action: str) -> Optional[str]:
    a = action.upper().strip()

    # First skip obvious non-trade actions.
    if any(skip in a for skip in SKIP_ACTION_WORDS):
        return None

    # Entry.
    if any(word in a for word in TRADE_ENTRY_WORDS):
        return "ENTRY"

    # Exit.
    if any(word in a for word in TRADE_EXIT_WORDS):
        return "EXIT"

    return None


def parse_trade_event(block: str) -> Optional[TradeEvent]:
    action = extract_first(r"^\s*ACTION:\s*(.+?)\s*$", block)
    if not action:
        return None

    event_type = classify_action(action)
    if event_type is None:
        return None

    time = extract_first(
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*E[SD]T",
        block,
    )

    symbol = extract_first(r"^\s*Symbol:\s*(.+?)\s*$", block)
    reason = extract_first(r"^\s*REASON:\s*(.+?)\s*$", block)
    position = extract_first(r"^\s*Position:\s*(.+?)\s*$", block)
    features = extract_first(r"^\s*Features:\s*(.+?)\s*$", block)

    prob_up, prob_down = extract_probabilities(block)
    open_price, close_price, price = extract_prices(block)

    return TradeEvent(
        event_type=event_type,
        action=action,
        time=time,
        symbol=symbol,
        price_open=open_price,
        price_close=close_price,
        price=price,
        prob_up=prob_up,
        prob_down=prob_down,
        reason=reason,
        position=position,
        features=features,
        raw_block=block.strip(),
    )


def side_from_action(action: str) -> str:
    a = action.upper()
    if "LONG" in a or a == "BUY":
        return "LONG"
    if "SHORT" in a or "SELL" in a:
        return "SHORT"
    return "?"


def fmt_price(x: Optional[float]) -> str:
    return "?" if x is None else f"${x:.2f}"


def fmt_prob(x: Optional[float]) -> str:
    return "?" if x is None else f"{x:.3f}"


def print_compact(events: List[TradeEvent], filepath: str):
    filename = os.path.basename(filepath)

    m = re.search(r"bot_(\d+)_(\w+)_", filename)
    bot_id = m.group(1) if m else "?"
    file_symbol = m.group(2) if m else "?"

    print("=" * 100)
    print(f"ENTRY/EXIT TRADE LOGS ONLY — Bot #{bot_id} {file_symbol}")
    print(f"File: {filepath}")
    print("=" * 100)

    if not events:
        print("No ENTRY/EXIT trade events found.")
        print("Only no-action logs may exist for this bot/log file.")
        return

    entries = sum(1 for e in events if e.event_type == "ENTRY")
    exits = sum(1 for e in events if e.event_type == "EXIT")

    print(f"Found {len(events)} trade event(s): {entries} entries, {exits} exits")
    print()

    open_trade = None
    trade_num = 0

    for e in events:
        side = side_from_action(e.action)
        symbol = e.symbol or file_symbol

        if e.event_type == "ENTRY":
            trade_num += 1
            open_trade = e

            print(f"🟢 ENTRY #{trade_num}")
            print(f"  Time:      {e.time}")
            print(f"  Symbol:    {symbol}")
            print(f"  Action:    {e.action}")
            print(f"  Side:      {side}")
            print(f"  Price:     {fmt_price(e.price)}  [OPEN={fmt_price(e.price_open)} CLOSE={fmt_price(e.price_close)}]")
            print(f"  Prob:      UP={fmt_prob(e.prob_up)} DOWN={fmt_prob(e.prob_down)}")
            if e.reason:
                print(f"  Reason:    {e.reason}")
            if e.position:
                print(f"  Position:  {e.position}")
            if e.features and e.features != "No feature data":
                print(f"  Features:  {e.features}")
            print()

        elif e.event_type == "EXIT":
            pnl = None
            pnl_text = ""

            if open_trade and open_trade.price is not None and e.price is not None:
                entry_side = side_from_action(open_trade.action)
                if entry_side == "LONG":
                    pnl = e.price - open_trade.price
                elif entry_side == "SHORT":
                    pnl = open_trade.price - e.price

                if pnl is not None:
                    pnl_text = f"  P&L/share: ${pnl:+.2f}"

            print(f"🔴 EXIT")
            print(f"  Time:      {e.time}")
            print(f"  Symbol:    {symbol}")
            print(f"  Action:    {e.action}")
            print(f"  Price:     {fmt_price(e.price)}  [OPEN={fmt_price(e.price_open)} CLOSE={fmt_price(e.price_close)}]")
            print(f"  Prob:      UP={fmt_prob(e.prob_up)} DOWN={fmt_prob(e.prob_down)}")
            if e.reason:
                print(f"  Reason:    {e.reason}")
            if e.position:
                print(f"  Position:  {e.position}")
            if pnl_text:
                print(pnl_text)
            print()

            open_trade = None


def print_raw(events: List[TradeEvent], filepath: str):
    print("=" * 100)
    print(f"RAW ENTRY/EXIT BLOCKS ONLY")
    print(f"File: {filepath}")
    print("=" * 100)

    if not events:
        print("No ENTRY/EXIT trade events found.")
        return

    for i, e in enumerate(events, 1):
        print()
        print("-" * 100)
        print(f"{i}. {e.event_type} | {e.action}")
        print("-" * 100)
        print(e.raw_block)


def analyze_log(filepath: str, raw: bool = False):
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    blocks = split_blocks(text)
    events = []

    for block in blocks:
        event = parse_trade_event(block)
        if event:
            events.append(event)

    if raw:
        print_raw(events, filepath)
    else:
        print_compact(events, filepath)


def main():
    parser = argparse.ArgumentParser(
        description="Print only ENTRY/EXIT trade logs for an AlgoMM bot."
    )
    parser.add_argument(
        "target",
        help="Bot ID like 709, or full path to bot_###_SYMBOL_AlgoMM.log",
    )
    parser.add_argument(
        "--data-root",
        default="/var/www/stockwicks/data",
        help="Root data folder. Default: /var/www/stockwicks/data",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw matching ENTRY/EXIT log blocks instead of compact summary.",
    )

    args = parser.parse_args()

    if os.path.exists(args.target):
        filepath = args.target
    elif args.target.isdigit():
        filepath = find_log_by_bot_id(args.target, args.data_root)
    else:
        print(f"Invalid target: {args.target}")
        print("Use either bot ID or full log path.")
        sys.exit(1)

    analyze_log(filepath, raw=args.raw)


if __name__ == "__main__":
    main()