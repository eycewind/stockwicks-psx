from app.trading.algos.base import AlgoDecision


class RiskManager:
    def approve(self, decision: AlgoDecision, context: object) -> AlgoDecision:
        # MVP placeholder. Later enforce daily loss, max size, broker status,
        # emergency stop, market hours, and live-trading disclaimer.
        return decision
