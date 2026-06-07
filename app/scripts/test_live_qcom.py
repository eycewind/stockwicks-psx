# save as test_live_qcom.py
from app.services.bot_order_trigger import trigger_bot_trade

trigger_bot_trade(
    bot_id=222,
    symbol="QCOM",
    side="SELL",
    qty=1.0,                    # small size for test
    order_type="MARKET",
    mirror_live=True,           # forces live even if bot flag off
    actor="debug_live_test"
)