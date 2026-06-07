# app/services/email_service.py
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from hashlib import sha256
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.notification_log import TradeNotificationLog

log = logging.getLogger(__name__)


class EmailService:
    def __init__(self):
        # SMTP config with sane defaults; override via app.config.settings
        self.smtp_host = getattr(settings, "SMTP_HOST", "localhost")
        self.smtp_port = int(getattr(settings, "SMTP_PORT", 25))
        self.username = getattr(settings, "SMTP_USERNAME", None)
        self.password = getattr(settings, "SMTP_PASSWORD", None)
        self.use_tls = bool(getattr(settings, "SMTP_USE_TLS", False))
        self.from_address = getattr(
            settings, "SMTP_FROM", "notifications@stockwicks.com"
        )
        self.public_base_url = getattr(
            settings, "PUBLIC_BASE_URL", "https://stockwicks.com"
        ).rstrip("/")

    # ---------------- core sender (multipart) ----------------
    def _send_email(
        self,
        to_email: str,
        subject: str,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
    ):
        """
        Sends a multipart/alternative email. If only body_text is passed and it looks
        like HTML, it's treated as HTML for backward compatibility with older calls.
        """
        # Back-compat: _send_email(email, subject, body) where body was HTML
        if body_html is None and body_text and "<html" in body_text.lower():
            body_html, body_text = body_text, None

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = to_email

        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        if body_html:
            msg.attach(MIMEText(body_html, "html"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as s:
                if self.use_tls:
                    s.starttls()
                if self.username:
                    s.login(self.username, self.password)
                s.send_message(msg)
                log.debug(
                    f"Email sent successfully to {to_email} with subject '{subject}'."
                )
        except smtplib.SMTPAuthenticationError as auth_err:
            log.error(
                f"SMTP Authentication failed for user {self.username}. "
                f"Check credentials. Error: {auth_err}"
            )
            raise
        except Exception as smtp_err:
            log.error(
                f"Failed to send email via SMTP "
                f"({self.smtp_host}:{self.smtp_port}). Error: {smtp_err}",
                exc_info=True,
            )
            raise

    # ---------------- templated senders ----------------
    def send_verification_email(self, email: str, verification_link: str):
        """Sends an email verification link to the user.

        verification_link must be a full URL, already including any client prefix,
        e.g. /clients/ashakil/auth/verify/<token>.
        """
        try:
            subject = "StockWicks Email Verification"
            body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <h2>Welcome to StockWicks!</h2>
    <p>Please verify your email by clicking the link below:</p>
    <p>
        <a href="{verification_link}" style="display: inline-block; background-color: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">
            Verify Email
        </a>
    </p>
    <p><b>Note:</b> If you don’t see this email, check your Spam folder.</p>
    <p>If you cannot click the button, copy and paste this link into your browser:</p>
    <p><a href="{verification_link}">{verification_link}</a></p>
</body>
</html>
""".strip()
            self._send_email(email, subject, body_html=body_html)
            log.info(f"📩 Verification email sent to {email}.")
        except Exception as e:
            log.error(
                f"❌ Failed to send verification email to {email}: {e}",
                exc_info=True,
            )

    def send_mm_max_pain_result(
        self, email: str, symbol: str, expiry: str, result: dict
    ):
        """Sends the Max Pain calculation result via email."""
        try:
            subject = f"Max Pain Prediction for {symbol.upper()} on {expiry}"

            rows = "".join(
                f"<tr><td>{r['strike']}</td><td>{r['call_oi']}</td><td>{r['put_oi']}</td><td>{r['total_pain']}</td></tr>"
                for r in result.get("strike_data", [])
            )

            body_html = f"""
<html>
  <body style="font-family: Arial, sans-serif; font-size:14px; color:#333;">
    <h2>Max Pain Prediction for {symbol.upper()} on {expiry}</h2>
    <p><strong>Max Pain Price:</strong> {result.get("max_pain_price", "N/A")}</p>
    <table border="1" cellpadding="6" cellspacing="0">
      <thead>
        <tr><th>Strike</th><th>Call OI</th><th>Put OI</th><th>Total Pain</th></tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    <p style="margin-top: 16px;">This is an automated prediction from the StockWicks platform.</p>
  </body>
</html>
""".strip()

            self._send_email(email, subject, body_html=body_html)
            log.info(f"📩 Max Pain email sent to {email} for {symbol}")
        except Exception as e:
            log.error(
                f"❌ Failed to send Max Pain email to {email}: {e}",
                exc_info=True,
            )

    def send_reset_email(self, email, reset_link):
        """Sends a password reset email with a reset link."""
        try:
            subject = "StockWicks Password Reset Request"
            body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <h2>Password Reset Requested</h2>
    <p>You requested a password reset. Click the button below to reset your password:</p>
    <p>
        <a href="{reset_link}" style="display: inline-block; background-color: #ff5733; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">
            Reset Password
        </a>
    </p>
    <p><b>Note:</b> This link is valid for a limited time.</p>
    <p>If you did not request this, please ignore this email.</p>
</body>
</html>
""".strip()
            self._send_email(email, subject, body_html=body_html)
            log.info(f"📩 Password reset email sent to {email}.")
        except Exception as e:
            log.error(
                f"❌ Failed to send password reset email to {email}: {e}",
                exc_info=True,
            )

    def send_username_email(self, email, username):
        """Sends an email with the user's username."""
        try:
            subject = "StockWicks Username Recovery"
            body_html = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
    <h2>Username Recovery</h2>
    <p>You requested your username. Your username is:</p>
    <p><b>{username}</b></p>
    <p>If you did not request this, please ignore this email.</p>
</body>
</html>
""".strip()
            self._send_email(email, subject, body_html=body_html)
            log.info(f"📩 Username recovery email sent to {email}.")
        except Exception as e:
            log.error(
                f"❌ Failed to send username recovery email to {email}: {e}",
                exc_info=True,
            )

    def send_stock_trade_notification(
        self, email, symbol, side, price, qty, interval, algo_name
    ):
        """Send a STOCK trade execution email (for equity bots)"""
        try:
            action = side.upper().replace("_TO_", " ").replace("_", " ")
            subject = f"Trading Bot: Stock Trade Executed - {action} {symbol}"

            body_text = (
                "Trading Bot Notification\n"
                f"A stock trade has been executed by your bot:\n\n"
                f"Symbol: {symbol}\n"
                f"Action: {action}\n"
                f"Quantity: {qty}\n"
                f"Price: ${float(price):.2f}\n"
                f"Interval: {interval}\n"
                f"Algo: {algo_name}\n\n"
                "This is an automated notification from StockWicks Trading Bot."
            )
            body_html = f"""
<html>
  <body style="font-family: Arial, Helvetica, sans-serif; font-size:14px; color:#222; line-height:1.4;">
    <h2 style="margin:0 0 12px 0;">Trading Bot: Stock Trade Executed</h2>
    <p style="margin:0 0 16px 0;">Your bot executed the following stock trade:</p>
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr><td style="padding:2px 16px 2px 0;"><strong>Symbol:</strong></td><td>{symbol}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Action:</strong></td><td>{action}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Quantity:</strong></td><td>{qty}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Price:</strong></td><td>${float(price):.2f}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Interval:</strong></td><td>{interval}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Algo:</strong></td><td>{algo_name}</td></tr>
    </table>
    <p style="margin:16px 0 0 0; color:#555;">This is an automated notification from StockWicks Trading Bot.</p>
  </body>
</html>
""".strip()
            self._send_email(email, subject, body_text=body_text, body_html=body_html)
            log.info(f"📩 Stock trade notification sent to {email} for {symbol}")
        except Exception as e:
            log.error(
                f"❌ Failed to send stock trade notification to {email}: {e}",
                exc_info=True,
            )

    def send_option_trade_notification(
        self,
        email,
        symbol,
        side,
        price,
        qty,
        interval,
        algo_name,
        strike,
        expiry,
        position_side,
        pnl: Optional[float] = None,
        stop_loss: Optional[float] = None,
        profit_target: Optional[float] = None,
    ):
        """Send an OPTION trade Opened/Closed email (for option bots)"""
        try:
            subject_action = "Closed" if pnl is not None else "Opened"
            subject = f"Trading Bot: Option Trade {subject_action} - {symbol}"

            strike_str = str(strike) if strike is not None else "[N/A]"
            expiry_str = str(expiry) if expiry is not None else "[N/A]"
            pos_side_str = str(position_side).upper() if position_side else "[N/A]"
            option_desc = f"{pos_side_str} {strike_str} {expiry_str}"
            action_line = f"{side.upper()}"

            pnl_line_text = (
                f"PnL: ${float(pnl):.2f}\n" if pnl is not None else ""
            )
            pnl_line_html = (
                f'<tr><td style="padding:2px 16px 2px 0;"><strong>PnL:</strong></td><td>${float(pnl):.2f}</td></tr>'
                if pnl is not None
                else ""
            )

            stop_loss_line_text = (
                f"Stop Loss: ${float(stop_loss):.2f}\n"
                if stop_loss is not None
                else ""
            )
            stop_loss_line_html = (
                f'<tr><td style="padding:2px 16px 2px 0;"><strong>Stop Loss:</strong></td><td>${float(stop_loss):.2f}</td></tr>'
                if stop_loss is not None
                else ""
            )

            profit_target_line_text = (
                f"Profit Target: ${float(profit_target):.2f}\n"
                if profit_target is not None
                else ""
            )
            profit_target_line_html = (
                f'<tr><td style="padding:2px 16px 2px 0;"><strong>Profit Target:</strong></td><td>${float(profit_target):.2f}</td></tr>'
                if profit_target is not None
                else ""
            )

            body_text = (
                "Trading Bot Notification\n"
                f"Your bot executed the following option trade:\n\n"
                f"Underlying: {symbol}\n"
                f"Option: {option_desc}\n"
                f"Action: {action_line}\n"
                f"Quantity: {qty}\n"
                f"{subject_action} Price: ${float(price):.2f}\n"
                f"{stop_loss_line_text}"
                f"{profit_target_line_text}"
                f"{pnl_line_text}"
                f"Interval: {interval}\n"
                f"Algo: {algo_name}\n\n"
                "This is an automated notification from StockWicks Trading Bot."
            )

            body_html = f"""
<!doctype html>
<html>
  <body style="font-family: Arial, Helvetica, sans-serif; font-size:14px; color:#222; line-height:1.4;">
    <h2 style="margin:0 0 12px 0;">Trading Bot: Option Trade {subject_action}</h2>
    <p style="margin:0 0 16px 0;">Your bot executed the following option trade:</p>
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr><td style="padding:2px 16px 2px 0;"><strong>Underlying:</strong></td><td>{symbol}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Option:</strong></td><td>{option_desc}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Action:</strong></td><td>{action_line}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Quantity:</strong></td><td>{qty}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>{subject_action} Price:</strong></td><td>${float(price):.2f}</td></tr>
      {stop_loss_line_html}
      {profit_target_line_html}
      {pnl_line_html}
      <tr><td style="padding:2px 16px 2px 0;"><strong>Interval:</strong></td><td>{interval}</td></tr>
      <tr><td style="padding:2px 16px 2px 0;"><strong>Algo:</strong></td><td>{algo_name}</td></tr>
    </table>
    <p style="margin:16px 0 0 0; color:#555;">This is an automated notification from StockWicks Trading Bot.</p>
  </body>
</html>
""".strip()

            self._send_email(email, subject, body_text=body_text, body_html=body_html)
            log.info(
                f"📩 Option trade {subject_action.lower()} notification sent to {email} for {symbol}"
            )
        except Exception as e:
            log.error(
                f"❌ Failed to send option trade notification to {email}: {e}",
                exc_info=True,
            )


# ===== Notification Log helpers (idempotency-friendly) =====

def _as_str_id(x) -> str:
    """Always store/compare trade_id as TEXT."""
    return "" if x is None else str(x)


def notification_already_sent(
    db,
    user_id: int,
    notification_type: str,
    trade_id,
    *,
    trade_type: str = "stock",
) -> bool:
    """
    Returns True if a notification entry exists for
    (user_id, trade_type, notification_type, trade_id).
    NOTE: trade_id is coerced to TEXT so DB comparisons are always text=text.
    """
    trade_id_str = _as_str_id(trade_id)
    try:
        sql = text(
            """
            SELECT 1
            FROM trade_notification_log
            WHERE user_id = :user_id
              AND trade_type = :trade_type
              AND notification_type = :notification_type
              AND trade_id = :trade_id
            LIMIT 1
        """
        )
        row = db.execute(
            sql,
            {
                "user_id": user_id,
                "trade_type": trade_type,
                "notification_type": notification_type,
                "trade_id": trade_id_str,
            },
        ).first()
        return row is not None
    except Exception as e:
        log.error(
            f"[EMAIL-SVC CheckLog] Failed checking notification log: {e}",
            exc_info=True,
        )
        return False


def log_notification(
    db,
    user_id: int,
    notification_type: str,
    trade_id,
    email_to: Optional[str],
    payload: dict,
    *,
    trade_type: str = "stock",
) -> None:
    """
    Inserts a notification row. Idempotency should be enforced by caller or DB unique index.
    NOTE: trade_id is coerced to TEXT. Uses UTC time for logging timestamp.
    """
    trade_id_str = _as_str_id(trade_id)
    sql = text(
        """
        INSERT INTO trade_notification_log (user_id, trade_id, trade_type, notification_type, sent_at)
        VALUES (:user_id, :trade_id, :trade_type, :notification_type, :sent_at)
        ON CONFLICT DO NOTHING
    """
    )
    try:
        sent_at_ts = datetime.utcnow()
        db.execute(
            sql,
            {
                "user_id": user_id,
                "trade_id": trade_id_str,
                "trade_type": trade_type,
                "notification_type": notification_type,
                "sent_at": sent_at_ts,
            },
        )
        db.commit()
        log.debug(
            f"[EMAIL-SVC Logged] Logged notification: "
            f"{user_id=}, {trade_type=}, {notification_type=}, {trade_id=}"
        )
    except IntegrityError:
        db.rollback()
        log.debug(
            f"[EMAIL-SVC Logged] Notification already logged (IntegrityError): "
            f"{user_id=}, {trade_type=}, {notification_type=}, {trade_id=}"
        )
    except Exception as e:
        db.rollback()
        log.error(
            f"[EMAIL-SVC Logged] Failed to log notification: {e}",
            exc_info=True,
        )