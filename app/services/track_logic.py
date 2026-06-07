# app.services.track_logic
# app.services.track_logic.py
# ✅ Final app.services.track_logic.py (fully fixed: email logic + deduplication + lock file to prevent race conditions)
import os
import pandas as pd
from datetime import datetime
from app.services.email_service import EmailService

email_service = EmailService()


def track_open_positions(user_id, symbol, interval, user_email):
    base_dir = f"/var/www/stockwicks/data/{user_id}"
    trades_file = os.path.join(base_dir, f"{user_id}_{symbol}_{interval}_trades.csv")
    notif_log_file = os.path.join(base_dir, f"{user_id}_Notifications_sent.csv")
    lock_file = os.path.join(base_dir, f"{user_id}_{symbol}_{interval}_email.lock")

    if not os.path.exists(trades_file):
        return

    if os.path.exists(lock_file):
        return  # Prevent race conditions

    try:
        with open(lock_file, "w") as lf:
            lf.write("lock")

        df = pd.read_csv(trades_file)
        if df.empty:
            return

        # Initialize email columns if missing
        if 'open_email_sent' not in df.columns:
            df['open_email_sent'] = ''
        if 'close_email_sent' not in df.columns:
            df['close_email_sent'] = ''

        # Process open trades
        open_trades = df[df['status'] == 'open']
        for idx, trade in open_trades.iterrows():
            if trade['open_email_sent'] != 'y' and not log_exists(notif_log_file, trade['TRX_ID'], 'open'):
                success = send_open_email(trade, notif_log_file, user_email)
                if success:
                    df.at[idx, 'open_email_sent'] = 'y'

        # Process closed trades
        closed_trades = df[df['status'] == 'closed']
        for idx, trade in closed_trades.iterrows():
            if trade['close_email_sent'] != 'y' and not log_exists(notif_log_file, trade['TRX_ID'], 'close'):
                success = send_close_email(trade, notif_log_file, user_email)
                if success:
                    df.at[idx, 'close_email_sent'] = 'y'

        df.to_csv(trades_file, index=False)

    finally:
        if os.path.exists(lock_file):
            os.remove(lock_file)

def send_open_email(trade, log_file, user_email):
    subject = f"New Trade Alert: {trade['symbol']} - {trade['interval']}"
    body = f"""New trade opened for {trade['symbol']}:
Type: {trade['type']}
Interval: {trade['interval']}
Trade Size: {trade.get('trade_size', 100.0)}
Entry Time: {trade['Entry_date_time']}
Entry Price: {trade['Entry_price']}"""
    try:
        email_service._send_email(user_email, subject, body)
        append_log(log_file, subject, body, trade['TRX_ID'], 'open', user_email)
        return True
    except Exception as e:
        print(f"❌ Failed to send OPEN email for TRX {trade['TRX_ID']}: {e}")
        return False

def send_close_email(trade, log_file, user_email):
    if log_exists(log_file, trade['TRX_ID'], 'close'):
        return

    subject = f"Trade Closed: {trade['symbol']} - {trade['interval']}"
    body = f"""Trade closed for {trade['symbol']}:
Type: {trade['type']}
Interval: {trade['interval']}
Trade Size: {trade.get('trade_size', 100.0)}
Entry Time: {trade['Entry_date_time']}
Entry Price: {trade['Entry_price']}
Exit Time: {trade['Exit_date_time']}
Exit Price: {trade['Exit_price']}
Profit: {trade['Profit']}
Status: {'Win' if trade['Profit'] > 0 else 'Loss'}"""
    try:
        email_service._send_email(user_email, subject, body)
        append_log(log_file, subject, body, trade['TRX_ID'], 'close', user_email)
    except Exception as e:
        print(f"❌ Failed to send CLOSE email for TRX {trade['TRX_ID']}: {e}")

def append_log(path, subject, body, trx_id, type_, user_email):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
    is_new_file = not os.path.exists(path)
    df_new = pd.DataFrame([{
        "Timestamp": now,
        "User Email": user_email,
        "TRX_ID": trx_id,
        "Type": type_.upper(),
        "Subject": subject,
        "Message Body": body.strip()
    }])
    if is_new_file:
        df_new.to_csv(path, index=False)
    else:
        df_old = pd.read_csv(path)
        df_combined = pd.concat([df_old, df_new], ignore_index=True)
        df_combined.to_csv(path, index=False)

def log_exists(path, trx_id, type_):
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_csv(path)
        return not df[(df["TRX_ID"] == trx_id) & (df["Type"] == type_.upper())].empty
    except Exception as e:
        print(f"⚠️ Failed to read log file for deduplication check: {e}")
        return False
