#!/usr/bin/env python3
"""
CLI to submit a trade via trade_service.execute_trade_signal

Examples:
  # Market buy 1 SPY, mirror following bot flag/env
  python trade_cli.py --bot-id 60 --symbol SPY --side BUY --qty 1

  # Force mirror even if paper didn't create a new trade (use with caution)
  python trade_cli.py --bot-id 59 --symbol SPY --side BUY --qty 1 --mirror true --force-mirror

  # Limit order after-hours (safer), with account override
  python trade_cli.py --bot-id 60 --symbol SPY --side BUY --qty 1 \
    --order-type LIMIT --limit 663.25 --extended-hours --mirror true --account-id 123456789
"""
import argparse
import json
import logging
import sys
import os

# --- Ensure project root is importable ---
PROJECT_ROOT = "/var/www/stockwicks"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.trade_service import execute_trade_signal  # noqa: E402

log = logging.getLogger("trade_cli")


def _bool_from_str(v: str | None) -> bool | None:
    if v is None:
        return None
    v = v.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Submit a trade via trade_service.execute_trade_signal")
    p.add_argument("--bot-id", type=int, required=True, help="Bot ID that owns the trade")
    p.add_argument("--symbol", type=str, required=True, help="Ticker symbol, e.g., SPY")
    p.add_argument("--side", type=str, required=True, choices=["BUY", "SELL"], help="BUY or SELL")
    p.add_argument("--qty", type=float, default=1.0, help="Quantity (default: 1.0)")

    p.add_argument("--order-type", type=str, default="MARKET",
                   choices=["MARKET", "LIMIT"], help="Order type (default: MARKET)")
    p.add_argument("--limit", type=float, default=None, help="Limit price (required for LIMIT)")
    p.add_argument("--tif", type=str, default="DAY", choices=["DAY", "GTC"], help="Time in force (default: DAY)")
    p.add_argument("--extended-hours", action="store_true", help="Allow extended hours")

    p.add_argument("--mirror", type=str, default=None,
                   help="Override mirroring: true/false. If omitted, uses bot flag / env")
    p.add_argument("--force-mirror", action="store_true",
                   help="Mirror to Schwab even if no new paper trade was created (use with caution)")
    p.add_argument("--allow-no-paper", action="store_true",
                   help="Alias for --force-mirror (allows mirroring without a new paper row)")

    p.add_argument("--account-id", type=str, default=None,
                   help="Override Schwab account id/number (optional)")
    p.add_argument("--actor", type=str, default="cli", help="Actor tag for audit/logging")
    p.add_argument("--verbose", "-v", action="count", default=0, help="Increase logging verbosity")
    return p.parse_args(argv)


def configure_logging(verbosity: int):
    level = logging.INFO if verbosity == 0 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)

    side = args.side.upper()
    order_type = args.order_type.upper()
    tif = args.tif.upper()
    symbol = args.symbol.upper()

    if order_type == "LIMIT" and args.limit is None:
        print("ERROR: --limit is required for LIMIT orders", file=sys.stderr)
        return 2

    mirror_override = _bool_from_str(args.mirror)
    allow_no_paper = bool(args.force_mirror or args.allow_no_paper)

    # Helpful hint if DRY RUN is enabled in this process
    if os.getenv("SCHWAB_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}:
        log.warning("[CLI] SCHWAB_DRY_RUN is enabled; orders will NOT be sent live.")

    log.info(
        f"[CLI] bot_id={args.bot_id} sym={symbol} side={side} qty={args.qty} "
        f"type={order_type} limit={args.limit} tif={tif} ext={args.extended_hours} "
        f"mirror_override={mirror_override} force_mirror={args.force_mirror} "
        f"allow_no_paper={args.allow_no_paper} account_id={args.account_id or '(auto)'} actor={args.actor}"
    )

    try:
        result = execute_trade_signal(
            bot_id=args.bot_id,
            side=side,
            order_type=order_type,
            qty=args.qty,
            limit_price=args.limit,
            time_in_force=tif,
            extended_hours=bool(args.extended_hours),
            mirror_live_override=mirror_override,             # None → follow bot flag/env
            symbol_override=symbol,
            actor=args.actor,
            account_id_override=args.account_id,              # optional account override
            mirror_even_if_no_paper=allow_no_paper,           # allow mirroring w/o new paper row
        )
    except Exception as e:
        log.exception("[CLI] trade submission failed")
        print(json.dumps({"ok": False, "error": str(e)}), flush=True)
        return 1

    out = {"ok": True}
    out.update(result or {})
    print(json.dumps(out, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
