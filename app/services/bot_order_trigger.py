# app/services/bot_order_trigger.py  (new helper)
import logging
from app.services.trade_service import execute_trade_signal

log = logging.getLogger(__name__)

def trigger_bot_trade(
    bot_id: int,
    symbol: str,
    side: str,                 # "BUY" | "SELL"
    qty: float = 1.0,
    order_type: str = "MARKET",
    limit_price: float | None = None,
    time_in_force: str = "DAY",
    extended_hours: bool = False,
    mirror_live: bool | None = None,   # pass True to force mirroring from bot
    actor: str | None = None,
):
    log.info(f"[BOT→TRADE] bot={bot_id} sym={symbol} side={side} qty={qty} type={order_type} mirror={mirror_live}")
    return execute_trade_signal(
        bot_id=bot_id,
        side=side,
        order_type=order_type,
        qty=qty,
        limit_price=limit_price,
        time_in_force=time_in_force,
        extended_hours=extended_hours,
        mirror_live_override=mirror_live,
        symbol_override=symbol,
        actor=actor or f"bot:{bot_id}",
    )
