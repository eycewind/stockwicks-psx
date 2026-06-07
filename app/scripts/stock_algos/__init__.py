#var/www/stockwicks/app/scripts/stock_algos/__init__.py
# Central source of truth for algo metadata & file tags
from dataclasses import dataclass
from typing import Dict

@dataclass(frozen=True)
class AlgoSpec:
    key: str
    file_tag: str
    runner_import: str
    runner_func: str = "run_algo"

REGISTRY: Dict[str, AlgoSpec] = {
    "algo1_smi":  AlgoSpec("algo1_smi",  "algo1_smi",  "app.scripts.stock_algos.algo1_SMI_runner"),
    "algo2":      AlgoSpec("algo2",      "algo2",      "app.scripts.stock_algos.algo2_simple_runner"),
    "algo3_macd": AlgoSpec("algo3_macd", "algo3_macd", "app.scripts.stock_algos.algo3_MACD_runner"),
}


# aliases coming from UI / old records
NAME_TO_KEY = {
    "algo1 smi": "algo1_smi",
    "smi": "algo1_smi",
    "1": "algo1_smi",
    "algo2": "algo2",
    "2": "algo2",
    "simple": "algo2",
    "algo3": "algo3_macd",
    "3": "algo3_macd",
    "macd": "algo3_macd",
}

def normalize_to_key(name: str) -> str:
    n = (name or "").strip().lower()
    return NAME_TO_KEY.get(n, n)
