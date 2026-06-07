# app/services/paper_trade_option_service.py
import logging
from typing import Optional
from sqlalchemy.orm import Session
from app.models.schwab_accounts import SchwabAccount
# You will need to create this function based on the Schwab API documentation for options
# from app.utils.schwab_trade import submit_option_order

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (LIVE_OPTION_SERVICE) %(message)s")
log = logging.getLogger(__name__)

def _resolve_default_account_id(db: Session, user_id: int) -> Optional[str]:
    """ Finds the default Schwab account hash or number for a user. """
    try:
        q = db.query(SchwabAccount).filter(SchwabAccount.user_id == user_id)
        # Prefer an explicitly marked default account
        default = q.filter(SchwabAccount.is_default == True).first()
        if default:
            return default.account_hash or default.account_number
        # Fallback to the most recently added account
        row = q.order_by(SchwabAccount.id.desc()).first()
        if row:
            return row.account_hash or row.account_number
    except Exception as e:
        log.error(f"Could not resolve Schwab account for user_id={user_id}: {e}")
    return None

def mirror_live_option_order(
    db: Session,
    *,
    user_id: int,
    contract_symbol: str,
    quantity: float,
    instruction: str, # e.g., BUY_TO_OPEN, SELL_TO_CLOSE
) -> Optional[str]:
    """
    Places a live option order via the Schwab Trader API.
    This is a placeholder and needs to be connected to your live trading SDK.
    """
    acct_id = _resolve_default_account_id(db, user_id)
    if not acct_id:
        log.error(f"[LIVE MIRROR] No Schwab account found for user_id={user_id}. Skipping live order.")
        return None

    log.info(f"[LIVE MIRROR] ==> Preparing to send live order for user {user_id}: "
             f"{instruction} {quantity}x {contract_symbol} on account {acct_id}")

    try:
        # ======================= IMPORTANT =======================
        # This is where you would call your actual live trading function.
        # It is commented out because it likely doesn't exist yet.
        #
        # order_id = submit_option_order(
        #     account_id=acct_id,
        #     contract_symbol=contract_symbol,
        #     instruction=instruction,
        #     quantity=float(quantity),
        #     order_type="MARKET",
        #     time_in_force="DAY"
        # )
        # log.info(f"[LIVE MIRROR] <== SUCCESS: Live order placed, OrderID: {order_id}")
        # return order_id
        #
        # =========================================================
        log.warning("[LIVE MIRROR] <== SKIPPED: 'submit_option_order' function is not yet implemented.")
        return "mock_order_id_12345" # Return a mock ID for now

    except Exception as e:
        log.error(f"[LIVE MIRROR] <== FAILED: submit_option_order failed for user {user_id}: {e}", exc_info=True)
        return None