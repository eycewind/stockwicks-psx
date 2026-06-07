#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/options/clear_spx_trades.py
"""
SPX 0DTE Database Cleanup Tool

Supports:
- Delete everything (--all)
- Delete by user (--user)
- Delete trades ON a date (--on-date YYYY-MM-DD)
- Delete trades BEFORE a date (--before-date YYYY-MM-DD)
- Keep only last N days (--keep-last N)  # e.g. 1, 7

Also:
- Optional backup CSVs (--backup)
- Optional skip confirmation (--force)
"""

import sys
from datetime import datetime, timedelta, date

# Add project path
sys.path.insert(0, "/var/www/stockwicks")

from app.database.connection import SessionLocal
from app.models.paper_spx_0dte import PaperSPXTradeHistory, PaperSPXOpenTrade, PaperSPXPick

from sqlalchemy import func, and_


# -----------------------------
# Helpers
# -----------------------------
def _parse_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _print_counts(db, label: str, history_q, open_q, picks_q):
    history_count = history_q.count()
    open_count = open_q.count()
    picks_count = picks_q.count()

    print(f"\n📊 {label}")
    print(f"   Trade History: {history_count} records")
    print(f"   Open Trades:   {open_count} records")
    print(f"   Picks:         {picks_count} records")

    return history_count, open_count, picks_count


def _confirm_or_cancel(prompt: str, confirm: bool):
    if not confirm:
        return True
    response = input(prompt).strip().lower()
    if response != "yes":
        print("❌ Operation cancelled")
        return False
    return True


def _delete_orphan_picks(db):
    """
    Deletes picks that have no remaining trade history and no remaining open trades.
    Adjust if your schema differs (e.g., if open trades don't reference pick_id).
    """
    # Picks referenced by trade history
    hist_pick_ids = db.query(PaperSPXTradeHistory.pick_id).filter(PaperSPXTradeHistory.pick_id.isnot(None)).distinct()

    # Picks referenced by open trades (if applicable)
    open_pick_ids = db.query(PaperSPXOpenTrade.pick_id).filter(PaperSPXOpenTrade.pick_id.isnot(None)).distinct()

    deleted = db.query(PaperSPXPick).filter(
        PaperSPXPick.id.notin_(hist_pick_ids),
        PaperSPXPick.id.notin_(open_pick_ids),
    ).delete(synchronize_session=False)

    return deleted


# -----------------------------
# Operations
# -----------------------------
def clear_all_trades(confirm=True):
    """Delete all historical trades, open trades, and picks."""
    db = SessionLocal()
    try:
        history_q = db.query(PaperSPXTradeHistory)
        open_q = db.query(PaperSPXOpenTrade)
        picks_q = db.query(PaperSPXPick)

        _print_counts(db, "Current database state:", history_q, open_q, picks_q)

        if not _confirm_or_cancel("\n⚠️  Are you sure you want to DELETE ALL records? (yes/no): ", confirm):
            return

        print("\n🗑️  Deleting records...")

        deleted_history = history_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_history} trade history records")

        deleted_open = open_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_open} open trade records")

        deleted_picks = picks_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_picks} pick records")

        db.commit()
        print("\n✅ All SPX 0DTE trades have been cleared successfully!")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
    finally:
        db.close()


def clear_by_user(user_id: int, confirm=True):
    """Clear trades for a specific user only."""
    db = SessionLocal()
    try:
        history_q = db.query(PaperSPXTradeHistory).filter(PaperSPXTradeHistory.user_id == user_id)
        open_q = db.query(PaperSPXOpenTrade).filter(PaperSPXOpenTrade.user_id == user_id)
        picks_q = db.query(PaperSPXPick).filter(PaperSPXPick.user_id == user_id)

        _print_counts(db, f"User {user_id} database state:", history_q, open_q, picks_q)

        if not _confirm_or_cancel(f"\n⚠️  Delete ALL records for user {user_id}? (yes/no): ", confirm):
            return

        deleted_history = history_q.delete(synchronize_session=False)
        deleted_open = open_q.delete(synchronize_session=False)
        deleted_picks = picks_q.delete(synchronize_session=False)

        db.commit()
        print(f"\n✅ Cleared user {user_id}: {deleted_history} history, {deleted_open} open, {deleted_picks} picks")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
    finally:
        db.close()


def clear_before_date(cutoff: date, user_id: int | None = None, confirm=True):
    """
    Delete trades with:
      - TradeHistory.closed_at < cutoff_date_start
      - OpenTrade.opened_at   < cutoff_date_start
      - Picks.created_at      < cutoff_date_start (only if orphaned after deletions)
    """
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())

    db = SessionLocal()
    try:
        history_q = db.query(PaperSPXTradeHistory).filter(PaperSPXTradeHistory.closed_at < cutoff_dt)
        open_q = db.query(PaperSPXOpenTrade).filter(PaperSPXOpenTrade.opened_at < cutoff_dt)
        picks_q = db.query(PaperSPXPick).filter(PaperSPXPick.created_at < cutoff_dt)

        if user_id is not None:
            history_q = history_q.filter(PaperSPXTradeHistory.user_id == user_id)
            open_q = open_q.filter(PaperSPXOpenTrade.user_id == user_id)
            picks_q = picks_q.filter(PaperSPXPick.user_id == user_id)

        label = f"Records BEFORE {cutoff.strftime('%Y-%m-%d')} (cutoff={cutoff_dt})"
        _print_counts(db, label + (f" for user {user_id}" if user_id else ":"), history_q, open_q, picks_q)

        if not _confirm_or_cancel(
            f"\n⚠️  Delete records BEFORE {cutoff.strftime('%Y-%m-%d')}"
            + (f" for user {user_id}" if user_id else "")
            + "? (yes/no): ",
            confirm,
        ):
            return

        print("\n🗑️  Deleting records...")

        deleted_history = history_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_history} trade history records")

        deleted_open = open_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_open} open trade records")

        # Clean orphan picks (optionally only those created before cutoff, if you prefer)
        # Here: we delete orphans globally, but it's safe because it only removes picks with no refs.
        deleted_orphans = _delete_orphan_picks(db)
        print(f"   ✅ Deleted {deleted_orphans} orphaned picks")

        db.commit()
        print("\n✅ Cleanup complete!")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
    finally:
        db.close()


def clear_on_date(target: date, user_id: int | None = None, confirm=True):
    """
    Delete trades on a specific calendar date (YYYY-MM-DD):
      - TradeHistory where DATE(closed_at) == target
      - OpenTrade   where DATE(opened_at) == target
    """
    db = SessionLocal()
    try:
        history_q = db.query(PaperSPXTradeHistory).filter(func.date(PaperSPXTradeHistory.closed_at) == target)
        open_q = db.query(PaperSPXOpenTrade).filter(func.date(PaperSPXOpenTrade.opened_at) == target)

        # Picks created on that date are not necessarily safe to delete,
        # so we only delete orphan picks after deleting trades.
        picks_q = db.query(PaperSPXPick).filter(func.date(PaperSPXPick.created_at) == target)

        if user_id is not None:
            history_q = history_q.filter(PaperSPXTradeHistory.user_id == user_id)
            open_q = open_q.filter(PaperSPXOpenTrade.user_id == user_id)
            picks_q = picks_q.filter(PaperSPXPick.user_id == user_id)

        label = f"Records ON {target.strftime('%Y-%m-%d')}"
        _print_counts(db, label + (f" for user {user_id}" if user_id else ":"), history_q, open_q, picks_q)

        if not _confirm_or_cancel(
            f"\n⚠️  Delete records ON {target.strftime('%Y-%m-%d')}"
            + (f" for user {user_id}" if user_id else "")
            + "? (yes/no): ",
            confirm,
        ):
            return

        print("\n🗑️  Deleting records...")

        deleted_history = history_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_history} trade history records")

        deleted_open = open_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_open} open trade records")

        deleted_orphans = _delete_orphan_picks(db)
        print(f"   ✅ Deleted {deleted_orphans} orphaned picks")

        db.commit()
        print("\n✅ Cleanup complete!")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
    finally:
        db.close()


def keep_only_last_n_days(n_days: int, user_id: int | None = None, confirm=True):
    """
    Keep only last N days of trades => delete anything older than now - N days.
    """
    if n_days <= 0:
        raise ValueError("--keep-last must be >= 1")

    cutoff_dt = datetime.now() - timedelta(days=n_days)
    cutoff_date = cutoff_dt.date()
    # We implement via clear_before_date(cutoff_date+1?) NO: keep last N days based on datetime cutoff.
    # Use datetime cutoff directly for precision.
    db = SessionLocal()
    try:
        history_q = db.query(PaperSPXTradeHistory).filter(PaperSPXTradeHistory.closed_at < cutoff_dt)
        open_q = db.query(PaperSPXOpenTrade).filter(PaperSPXOpenTrade.opened_at < cutoff_dt)
        picks_q = db.query(PaperSPXPick).filter(PaperSPXPick.created_at < cutoff_dt)

        if user_id is not None:
            history_q = history_q.filter(PaperSPXTradeHistory.user_id == user_id)
            open_q = open_q.filter(PaperSPXOpenTrade.user_id == user_id)
            picks_q = picks_q.filter(PaperSPXPick.user_id == user_id)

        label = f"Delete records older than last {n_days} day(s) (cutoff={cutoff_dt.strftime('%Y-%m-%d %H:%M:%S')})"
        _print_counts(db, label + (f" for user {user_id}" if user_id else ":"), history_q, open_q, picks_q)

        if not _confirm_or_cancel(
            f"\n⚠️  Keep ONLY last {n_days} day(s) => delete older records"
            + (f" for user {user_id}" if user_id else "")
            + "? (yes/no): ",
            confirm,
        ):
            return

        print("\n🗑️  Deleting records...")

        deleted_history = history_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_history} trade history records")

        deleted_open = open_q.delete(synchronize_session=False)
        print(f"   ✅ Deleted {deleted_open} open trade records")

        deleted_orphans = _delete_orphan_picks(db)
        print(f"   ✅ Deleted {deleted_orphans} orphaned picks")

        db.commit()
        print("\n✅ Cleanup complete!")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
    finally:
        db.close()


def backup_data():
    """Backup data before deletion"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"spx_trades_backup_{timestamp}.csv"
    open_backup_file = f"spx_open_backup_{timestamp}.csv"
    picks_backup_file = f"spx_picks_backup_{timestamp}.csv"

    db = SessionLocal()
    try:
        import csv

        # Backup trade history
        with open(backup_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "user_id", "symbol", "side", "entry", "exit", "pnl", "closed_at"])
            for t in db.query(PaperSPXTradeHistory).all():
                writer.writerow([t.id, t.user_id, t.occ_symbol, t.position_side, t.entry_price, t.exit_price, t.pnl_usd, t.closed_at])

        # Backup open trades
        with open(open_backup_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "user_id", "symbol", "side", "entry", "current", "opened_at"])
            for t in db.query(PaperSPXOpenTrade).all():
                writer.writerow([t.id, t.user_id, t.occ_symbol, t.position_side, t.entry_price, t.current_mark_price, t.opened_at])

        # Backup picks
        with open(picks_backup_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "user_id", "symbol", "side", "strike", "entry", "confidence", "created_at"])
            for p in db.query(PaperSPXPick).all():
                writer.writerow([p.id, p.user_id, p.occ_symbol, p.position_side, p.strike, p.entry_price, p.confidence, p.created_at])

        print(f"\n📦 Backup created:")
        print(f"   ✅ {backup_file}")
        print(f"   ✅ {open_backup_file}")
        print(f"   ✅ {picks_backup_file}")
        return True

    except Exception as e:
        print(f"❌ Backup failed: {e}")
        return False
    finally:
        db.close()


# -----------------------------
# CLI
# -----------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Clear SPX 0DTE trades from database (safe + flexible)")

    parser.add_argument("--user", type=int, help="Apply operation to this user_id only")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--backup", action="store_true", help="Backup data before deleting")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="DELETE EVERYTHING (history, open trades, picks)")
    mode.add_argument("--before-date", type=str, help="Delete records BEFORE date (YYYY-MM-DD)")
    mode.add_argument("--on-date", type=str, help="Delete records ON date (YYYY-MM-DD)")
    mode.add_argument("--keep-last", type=int, help="Keep only last N days (e.g., 1 or 7)")

    args = parser.parse_args()

    print("🧹 SPX 0DTE Database Cleanup Tool")
    print("=" * 50)

    if args.backup:
        if not backup_data():
            print("❌ Backup failed. Aborting...")
            sys.exit(1)

    confirm = not args.force

    if args.all:
        clear_all_trades(confirm=confirm)

    elif args.before_date:
        cutoff = _parse_yyyy_mm_dd(args.before_date)
        clear_before_date(cutoff=cutoff, user_id=args.user, confirm=confirm)

    elif args.on_date:
        target = _parse_yyyy_mm_dd(args.on_date)
        clear_on_date(target=target, user_id=args.user, confirm=confirm)

    elif args.keep_last is not None:
        keep_only_last_n_days(n_days=args.keep_last, user_id=args.user, confirm=confirm)