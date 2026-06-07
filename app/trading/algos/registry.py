from app.trading.algos.base import BaseStrategy


ALGO_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str, strategy_cls: type[BaseStrategy]) -> None:
    if not name:
        raise ValueError("Strategy name is required")
    ALGO_REGISTRY[name] = strategy_cls


def get_strategy(algo_name: str) -> BaseStrategy:
    if algo_name not in ALGO_REGISTRY:
        raise ValueError(f"Unknown algo: {algo_name}")
    return ALGO_REGISTRY[algo_name]()
