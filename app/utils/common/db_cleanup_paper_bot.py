# scripts/tools/db_cleanup_paper_bot.py
# /var/www/stockwicks/app/utils/db_cleanup_paper_bot.py
import sys
import argparse
import psycopg2
from psycopg2.extras import DictCursor

# Hard-coded DB URL
DATABASE_URL = "postgresql://stockwicks_user:Stockwick2024@localhost/stockwicks"

BOT_STOCK = {
    "bots": "public.paper_stock_trade_bots",
    "open": "public.paper_stock_bot_open_trades",
    "hist": "public.paper_stock_bot_trade_history",
}
BOT_OPTION = {
    "bots": "public.paper_option_trade_bots",
    "open": "public.paper_option_bot_open_trades",
    "hist": "public.paper_option_bot_trade_history",
}

# Optional known dependents (manual trade tables, etc.)
KNOWN_DEPENDENTS = [
    "public.paper_stock_trades",
    "public.paper_option_trades",
]

def conn():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"ERROR: Could not connect to DB: {e}", file=sys.stderr)
        sys.exit(2)

def fq_to_schema_table(fq):
    if "." in fq:
        schema, table = fq.split(".", 1)
        return schema, table
    return "public", fq

def fetchval(c, sql, params=None):
    with c.cursor() as cur:
        cur.execute(sql, params or ())
        r = cur.fetchone()
        return r[0] if r else None

def count_rows(c, fq_table):
    return fetchval(c, f"SELECT COUNT(*) FROM {fq_table}")

def list_fk_children(c, parent_fq_table):
    """List fully-qualified child tables that have FKs to parent."""
    pschema, ptable = fq_to_schema_table(parent_fq_table)
    sql = """
    SELECT DISTINCT child_ns.nspname AS child_schema, child.relname AS child_table
    FROM pg_constraint con
    JOIN pg_class child ON child.oid = con.conrelid
    JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
    JOIN pg_class parent ON parent.oid = con.confrelid
    JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
    WHERE con.contype = 'f'
      AND parent_ns.nspname = %s
      AND parent.relname   = %s;
    """
    with c.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(sql, (pschema, ptable))
        rows = cur.fetchall()
    return [f'{r["child_schema"]}.{r["child_table"]}' for r in rows]

def confirm_or_die(msg, assume_yes=False):
    if assume_yes:
        return
    ans = input(f"{msg} Type 'yes' to continue: ").strip().lower()
    if ans != "yes":
        print("Aborted.")
        sys.exit(1)

def ensure_guards(c):
    sqls = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_stock_open_one_per_bot ON public.paper_stock_bot_open_trades (bot_id);",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_option_open_one_per_bot ON public.paper_option_bot_open_trades (bot_id);",
        "ALTER TABLE public.paper_stock_bot_open_trades  ADD CONSTRAINT IF NOT EXISTS chk_stock_qty_pos  CHECK (qty > 0);",
        "ALTER TABLE public.paper_option_bot_open_trades ADD CONSTRAINT IF NOT EXISTS chk_option_qty_pos CHECK (qty > 0);",
    ]
    with c, c.cursor() as cur:
        for s in sqls:
            cur.execute(s)

def reset_bot(c, tnames, bot_id, dry=False, verbose=False):
    if verbose:
        print(f"- deleting OPEN + HISTORY for bot #{bot_id} in {tnames['bots']}")
    if dry:
        return
    with c, c.cursor() as cur:
        cur.execute(f"DELETE FROM {tnames['open']} WHERE bot_id = %s;", (bot_id,))
        cur.execute(f"DELETE FROM {tnames['hist']} WHERE bot_id = %s;", (bot_id,))

def remove_bot(c, tnames, bot_id, dry=False, verbose=False):
    if verbose:
        print(f"- deleting OPEN + HISTORY + BOT #{bot_id} in {tnames['bots']}")
    if dry:
        return
    with c, c.cursor() as cur:
        cur.execute(f"DELETE FROM {tnames['open']} WHERE bot_id = %s;", (bot_id,))
        cur.execute(f"DELETE FROM {tnames['hist']} WHERE bot_id = %s;", (bot_id,))
        cur.execute(f"DELETE FROM {tnames['bots']} WHERE id = %s;", (bot_id,))

def cleanup_all(c, tnames, dry=False, verbose=False):
    if verbose:
        print(f"- deleting OPEN + HISTORY for {tnames['bots']}")
    if dry:
        return
    with c, c.cursor() as cur:
        cur.execute(f"DELETE FROM {tnames['open']};")
        cur.execute(f"DELETE FROM {tnames['hist']};")

def _truncate_cascade_family(c, tnames, dry=False, verbose=False):
    if verbose:
        print(f"- TRUNCATE CASCADE family for {tnames['bots']}")
    if dry:
        return
    with c, c.cursor() as cur:
        cur.execute(f"TRUNCATE {tnames['open']}  RESTART IDENTITY CASCADE;")
        cur.execute(f"TRUNCATE {tnames['hist']}  RESTART IDENTITY CASCADE;")
        cur.execute(f"TRUNCATE {tnames['bots']}  RESTART IDENTITY CASCADE;")

def _delete_family_bot_only(c, tnames, dry=False, verbose=False):
    """Delete only the 3 bot tables: open -> hist -> bots."""
    if verbose:
        print(f"- delete (bot-only) family for {tnames['bots']}")
    if dry:
        return
    with c, c.cursor() as cur:
        cur.execute(f"DELETE FROM {tnames['open']};")
        cur.execute(f"DELETE FROM {tnames['hist']};")
        cur.execute(f"DELETE FROM {tnames['bots']};")

def _delete_family_with_dependents(c, tnames, dry=False, verbose=False):
    """Delete FK-dependent child tables first, then open -> hist -> bots."""
    parent = tnames["bots"]
    children = list_fk_children(c, parent)

    # include known dependents if they exist
    for k in KNOWN_DEPENDENTS:
        try:
            count_rows(c, k)  # probe existence
            if k not in children:
                children.append(k)
        except Exception:
            pass

    if verbose:
        if children:
            print(f"- discovered dependents for {parent}:")
            for ch in children:
                print(f"    · {ch}")
        else:
            print(f"- no dependents discovered for {parent}")

    if dry:
        return

    with c, c.cursor() as cur:
        for ch in children:
            try:
                if verbose:
                    print(f"  deleting from {ch}")
                cur.execute(f"DELETE FROM {ch};")
            except Exception as e:
                print(f"WARNING: could not delete from {ch}: {e}", file=sys.stderr)

        cur.execute(f"DELETE FROM {tnames['open']};")
        cur.execute(f"DELETE FROM {tnames['hist']};")
        cur.execute(f"DELETE FROM {tnames['bots']};")

def family_counts(c, tnames):
    return {
        "bots": count_rows(c, tnames["bots"]),
        "open": count_rows(c, tnames["open"]),
        "hist": count_rows(c, tnames["hist"]),
    }

def nuke_all(c, tnames, mode="bot-only", dry=False, verbose=False):
    """
    mode:
      - "bot-only": delete only open -> hist -> bots
      - "with-dependents": delete FK dependents first, then open -> hist -> bots
      - "cascade": TRUNCATE ... CASCADE the three tables
    """
    if verbose:
        print(f"[nuke] family={tnames['bots']} mode={mode}")
    if mode == "cascade":
        _truncate_cascade_family(c, tnames, dry=dry, verbose=verbose)
    elif mode == "with-dependents":
        _delete_family_with_dependents(c, tnames, dry=dry, verbose=verbose)
    else:
        _delete_family_bot_only(c, tnames, dry=dry, verbose=verbose)

def main():
    ap = argparse.ArgumentParser(description="Cleanup tools for bot tables (stock/option).")
    # Global flags (these were missing previously)
    ap.add_argument("--verbose", action="store_true", help="Print detailed actions.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen, make no changes.")

    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("ensure-guards", help="Create protective DB constraints/indexes.")
    g.add_argument("--yes", action="store_true", help="Do not prompt.")

    r1 = sub.add_parser("reset-bot", help="Delete open+history for a single bot (keep config).")
    r1.add_argument("--type", choices=["stock","option"], required=True)
    r1.add_argument("--bot-id", type=int, required=True)
    r1.add_argument("--yes", action="store_true")

    r2 = sub.add_parser("remove-bot", help="Delete bot config AND its open/history.")
    r2.add_argument("--type", choices=["stock","option"], required=True)
    r2.add_argument("--bot-id", type=int, required=True)
    r2.add_argument("--yes", action="store_true")

    ca = sub.add_parser("cleanup-all", help="Delete open+history (keep bot configs).")
    ca.add_argument("--type", choices=["stock","option"], required=True)
    ca.add_argument("--yes", action="store_true")

    na = sub.add_parser("nuke-all", help="Delete bots + open + history (full reset).")
    na.add_argument("--type", choices=["stock","option","both"], required=True)
    na.add_argument("--mode", choices=["bot-only","with-dependents","cascade"], default="bot-only",
                    help="bot-only (default), with-dependents (delete FK children first), or cascade (TRUNCATE CASCADE).")
    na.add_argument("--yes", action="store_true")

    args = ap.parse_args()
    c = conn()

    try:
        if args.cmd == "ensure-guards":
            confirm_or_die("This will create indexes/constraints if missing.", args.yes)
            ensure_guards(c)
            print("OK: guards ensured.")
            return

        def pick(tt):
            return BOT_STOCK if tt == "stock" else BOT_OPTION

        if args.cmd == "reset-bot":
            T = pick(args.type)
            if args.verbose:
                before = family_counts(c, T)
                print(f"Before: {before}")
            confirm_or_die(f"Reset {args.type} bot #{args.bot_id}: delete OPEN + HISTORY (keep bot config).", args.yes)
            reset_bot(c, T, args.bot_id, dry=args.dry_run, verbose=args.verbose)
            if args.verbose and not args.dry_run:
                after = family_counts(c, T)
                print(f"After:  {after}")
            print(f"OK: reset {args.type} bot #{args.bot_id}.")
            return

        if args.cmd == "remove-bot":
            T = pick(args.type)
            if args.verbose:
                before = family_counts(c, T)
                print(f"Before: {before}")
            confirm_or_die(f"REMOVE {args.type} bot #{args.bot_id}: delete OPEN + HISTORY + BOT CONFIG.", args.yes)
            remove_bot(c, T, args.bot_id, dry=args.dry_run, verbose=args.verbose)
            if args.verbose and not args.dry_run:
                after = family_counts(c, T)
                print(f"After:  {after}")
            print(f"OK: removed {args.type} bot #{args.bot_id}.")
            return

        if args.cmd == "cleanup-all":
            T = pick(args.type)
            if args.verbose:
                before = family_counts(c, T)
                print(f"Before: {before}")
            confirm_or_die(f"Cleanup ALL {args.type} OPEN + HISTORY (keep bots).", args.yes)
            cleanup_all(c, T, dry=args.dry_run, verbose=args.verbose)
            if args.verbose and not args.dry_run:
                after = family_counts(c, T)
                print(f"After:  {after}")
            print(f"OK: cleaned {args.type} open+history.")
            return

        if args.cmd == "nuke-all":
            families = [BOT_STOCK, BOT_OPTION] if args.type == "both" else [pick(args.type)]

            if args.verbose:
                print("Families to wipe:", [f["bots"] for f in families])
                print("Mode:", args.mode)
                for T in families:
                    print(f"  Before {T['bots']}: {family_counts(c, T)}")

            warn = "This may also wipe dependent tables!" if args.mode in ("with-dependents", "cascade") else "Only the 3 bot tables will be cleared."
            confirm_or_die(f"NUKE {args.type.upper()} (mode={args.mode}): bots + open + history. {warn}", args.yes)

            for T in families:
                nuke_all(c, T, mode=args.mode, dry=args.dry_run, verbose=args.verbose)

            if args.verbose and not args.dry_run:
                for T in families:
                    print(f"  After  {T['bots']}: {family_counts(c, T)}")

            print(f"OK: nuked {args.type} (mode={args.mode}).")
            return

    finally:
        c.close()

if __name__ == "__main__":
    main()
