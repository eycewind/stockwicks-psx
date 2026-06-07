import importlib
import pandas as pd
import sys, os

# CLI: symbol, trade size, user id
symbol = sys.argv[1].upper()
trade_size = float(sys.argv[2])
user_id = sys.argv[3]

# List of algo modules to run
ALGO_MODULES = [
    "app.scripts.stocks.AI_algo1_SMI",
    "app.scripts.stocks.AI_algo2_MACD",
    "app.scripts.stocks.AI_algo3_RSI",
    "app.scripts.stocks.AI_algo4_VWAP",
    "app.scripts.stocks.AI_algo5_Bollinger",
]

all_results = []

for module_name in ALGO_MODULES:
    mod = importlib.import_module(module_name)
    results = mod.evaluate_all_intervals()
    for r in results:
        r["algo"] = module_name.split(".")[-1]   # tag which algo
        all_results.append(r)

# Choose best across all algos & intervals
df = pd.DataFrame(all_results)
df = df.sort_values(
    by=['success_rate','total_return','trades','interval'],
    ascending=[False,False,False,True]
)
best = df.iloc[0]

print("Best choice:")
print(best.to_dict())
