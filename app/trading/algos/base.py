from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AlgoDecision:
    action: str
    reason: str
    price_used: float
    confidence: Optional[float] = None
    prob_up: Optional[float] = None
    prob_down: Optional[float] = None
    features: Dict[str, Any] = field(default_factory=dict)
    thresholds: Dict[str, Any] = field(default_factory=dict)
    risk: Dict[str, Any] = field(default_factory=dict)
    debug: Dict[str, Any] = field(default_factory=dict)


class BaseStrategy:
    name = "BASE"
    lookback_days = 10

    def generate_signal(self, context: Any) -> AlgoDecision:
        raise NotImplementedError
