# scripts/tools/db_snapshot_paper_bot.py
# /var/www/stockwicks/app/utils/db_snapshot.py
import sys, argparse, json, csv, os
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras

# Default DB URL (override with --db-url)
DEFAULT_DB_URL = "postgresql://stockwicks_user:Stockwick2024@localhost/stockwicks"

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

def conn(db_url: str):
    try:
        return psycopg2.connect(db_url)
    except Exception as e:
        print(f"ERROR: Could not connect to DB: {e}", file=sys.stderr)
        sys.exit(2)

def table_columns(c, full_table_name: str):
    schema, _, table = full_table_name.partition(".")
    schema = schema or "public"
    table = table.strip('"')
    sql = """
      SELECT column_name
      FROM information_schema.columns
      WHERE table_schema = %s AND table_name = %s
    """
    with c.cursor() as cur:
        cur.execute(sql, (schema, table))
        return {r[0].lower() for r in cur.fetchall()}

def q_all(c, sql, args=None):
    with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, args or ())
        return [dict(r) for r in cur.fetchall()]

def q_one(c, sql, args=None):
    with c.cursor() as cur:
        cur.execute(sql, args or ())
        r = cur.fetchone()
        return r[0] if r else None

def counts(c, tnames, bot_cols):
    out = {
        "bots": q_one(c, f"SELECT COUNT(*) FROM {tnames['bots']}"),
        "open": q_one(c, f"SELECT COUNT(*) FROM {tnames['open']}"),
        "history": q_one(c, f"SELECT COUNT(*) FROM {tnames['hist']}"),
    }
    if "enabled" in bot_cols:
        out["enabled_bots"] = q_one(c, f"SELECT COUNT(*) FROM {tnames['bots']} WHERE enabled = true")
    else:
        out["enabled_bots"] = None
    return out

def list_enabled_bots(c, tnames, bot_cols):
    select_cols = ["id"]
    if "enabled" in bot_cols:
        select_cols.append("enabled")
    # asset field
    if "symbol" in bot_cols:
        select_cols.append("symbol AS asset")
    elif "contract_symbol" in bot_cols:
        select_cols.append("contract_symbol AS asset")
    elif "underlying" in bot_cols:
        select_cols.append("underlying AS asset")
    # others
    if "interval" in bot_cols:
        select_cols.append("interval")
    if "algo" in bot_cols:
        select_cols.append("algo")

    where = "WHERE enabled = true" if "enabled" in bot_cols else ""
    sql = f"SELECT {', '.join(select_cols)} FROM {tnames['bots']} {where} ORDER BY id"
    return q_all(c, sql)

def _order_clause(cols: set, prefer: list[str]):
    for col in prefer:
        if col in cols:
            return f"ORDER BY {col} DESC NULLS LAST, id DESC"
    return "ORDER BY id DESC"

def list_open_positions(c, tnames, open_cols):
    # Normalize asset column
    if "symbol" in open_cols:
        asset_expr = "symbol AS asset"
    elif "contract_symbol" in open_cols:
        asset_expr = "contract_symbol AS asset"
    else:
        asset_expr = "NULL::text AS asset"

    sel = ["id", "bot_id", asset_expr]
    for col in ("qty", "avg_price", "stop", "take_profit", "opened_at", "created_at"):
        if col in open_cols:
            sel.append(col)

    order_by = _order_clause(open_cols, ["opened_at", "created_at"])
    sql = f"SELECT {', '.join(sel)} FROM {tnames['open']} {order_by} LIMIT 100"
    return q_all(c, sql)

def list_recent_history(c, tnames, hist_cols, limit=25):
    if "symbol" in hist_cols:
        asset_expr = "symbol AS asset"
    elif "contract_symbol" in hist_cols:
        asset_expr = "contract_symbol AS asset"
    else:
        asset_expr = "NULL::text AS asset"

    sel = ["id", "bot_id", asset_expr]
    for col in ("qty", "entry_price", "exit_price", "opened_at", "closed_at", "updated_at", "created_at", "reason"):
        if col in hist_cols:
            sel.append(col)

    order_by = _order_clause(hist_cols, ["closed_at", "updated_at", "created_at"])
    sql = f"SELECT {', '.join(sel)} FROM {tnames['hist']} {order_by} LIMIT %s"
    return q_all(c, sql, (limit,))

def write_csv(rows, path):
    if not rows:
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

def main():
    ap = argparse.ArgumentParser(description="Snapshot bot DB state (stock + option).")
    ap.add_argument("--export-dir", help="If set, writes CSVs to this directory.")
    ap.add_argument("--history-limit", type=int, default=25)
    ap.add_argument("--db-url", default=DEFAULT_DB_URL, help="Override DB URL")
    args = ap.parse_args()

    c = conn(args.db_url)
    try:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stock": {},
            "option": {}
        }

        for label, T in (("stock", BOT_STOCK), ("option", BOT_OPTION)):
            bot_cols = table_columns(c, T["bots"])
            open_cols = table_columns(c, T["open"])
            hist_cols = table_columns(c, T["hist"])

            report[label]["counts"] = counts(c, T, bot_cols)
            report[label]["enabled_bots"] = list_enabled_bots(c, T, bot_cols)
            report[label]["open_positions"] = list_open_positions(c, T, open_cols)
            report[label]["recent_history"] = list_recent_history(c, T, hist_cols, limit=args.history_limit)

            if args.export_dir:
                os.makedirs(args.export_dir, exist_ok=True)
                write_csv(report[label]["enabled_bots"], os.path.join(args.export_dir, f"{label}_enabled_bots.csv"))
                write_csv(report[label]["open_positions"], os.path.join(args.export_dir, f"{label}_open_positions.csv"))
                write_csv(report[label]["recent_history"], os.path.join(args.export_dir, f"{label}_history.csv"))

        print(json.dumps(report, indent=2, default=str))
    finally:
        c.close()

if __name__ == "__main__":
    main()
