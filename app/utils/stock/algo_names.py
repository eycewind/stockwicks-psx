#/var/www/stockwicks/app/utils/stock/algo_names.py
import os

# Hardcoded mapping from runner filenames (basename only) to algo names
ALGO_NAME_MAP = {
    "algo1_trades_paper_runner": "Algo1",
    "algo2_trades_paper_runner": "Algo2",
    "algo3_trades_paper_runner": "Algo3",
    # extend later up to Algo10
    # "algo4_trades_paper_runner": "Algo4",
    # "algo5_trades_paper_runner": "Algo5",
    # ...
}

def get_algo_name_from_file(file_path: str) -> str:
    """
    Resolve algo name based on the runner script filename.
    Example:
      /app/scripts/stock_algos/algo1_trades_paper_runner.py -> "Algo1"
    """
    fname = os.path.basename(file_path).replace(".py", "")
    return ALGO_NAME_MAP.get(fname, "Unknown")
