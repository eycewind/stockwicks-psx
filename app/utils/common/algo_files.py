#/var/www/stockwicks/app/utils/common/algo_files.py
import os
from app.scripts.stock_algos import REGISTRY

BASE_DIR = "/var/www/stockwicks/data"

def algo_file_paths(user_id: int, symbol: str, algo_key: str):
    spec = REGISTRY[algo_key]
    tag = spec.file_tag
    base = os.path.join(BASE_DIR, f"{user_id}")
    prefix = f"{user_id}_{symbol}_{tag}"
    return {
        "notify_open": os.path.join(base, f"{prefix}_notify_open.csv"),
        "long":        os.path.join(base, f"{prefix}_long_trades.csv"),
        "short":       os.path.join(base, f"{prefix}_short_trades.csv"),
    }
