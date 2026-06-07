from app.trading.algos.base import AlgoDecision


class TradeRouter:
    def route(self, decision: AlgoDecision, context: object) -> dict:
        # MVP placeholder. Later route to PaperExecutor first, then SchwabExecutor
        # only when live mirroring and risk checks are enabled.
        return {
            "ok": True,
            "mode": "paper",
            "action": decision.action,
            "reason": decision.reason,
        }
