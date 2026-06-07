# AI_eveluate_stg_v2.py
import sys
import os
import subprocess
import pandas as pd
import concurrent.futures
import logging
from dotenv import load_dotenv
import json

# Load environment variables
load_dotenv()
os.environ['PYTHONIOENCODING'] = 'utf-8'
logging.basicConfig(level=logging.INFO)

# ⌨️ Command-line arguments
symbol = sys.argv[1]
trade_size = float(sys.argv[2])
user_id = sys.argv[3]

# 🗂️ Output paths
DATA_DIR = os.getenv('DATA_DIR', '/var/www/stockwicks/data')
user_data_dir = os.path.join(DATA_DIR, str(user_id))
os.makedirs(user_data_dir, exist_ok=True)

# 🔁 Schwab-supported intervals ONLY
intervals_to_test = ['1min', '5min', '10min', '15min', '30min','1d']

# 🛠️ Utility to determine trade type from interval
def get_trade_type(interval):
    fast = ['1min', '2min', '3min', '4min']
    return "Day Trade" if interval in fast else "Swing Trade"

# 📥 Run subprocess for each interval
script_base = os.path.dirname(os.path.abspath(__file__))  # Gets /var/www/stockwicks/app/scripts
trades_script = os.path.join(script_base, "trades_working.py")

def evaluate_interval(interval):
    try:
        # Ensure subprocess uses the project root for imports
        env = os.environ.copy()
        script_base = os.path.dirname(os.path.abspath(__file__))
        env['PYTHONPATH'] = os.path.abspath(os.path.join(script_base, '../..'))

        # Run the trade analysis script for this interval
        subprocess.run([
            "python3", trades_script, symbol, interval, str(trade_size), str(user_id)
        ], check=True, env=env)

        # Path to the interval’s output summary
        summary_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_summary.csv")
        if not os.path.exists(summary_path):
            logging.warning(f"No summary file found for {interval}")
            return None

        # Parse summary output for results
        df = pd.read_csv(summary_path)
        total_profit = 0
        total_success_rate = 0
        count = 0
        for _, row in df.iterrows():
            val = row.get('Total_profit', 0)
            if isinstance(val, str):
                val = val.replace('$', '').strip()
            try:
                total_profit += float(val)
            except Exception:
                pass
            try:
                total_success_rate += float(row.get('SuccessRate', '0').replace('%', ''))
                count += 1
            except Exception:
                pass

        avg_success_rate = (total_success_rate / count) if count > 0 else 0

        return {
            'interval': interval,
            'total_return': total_profit,
            'success_rate': avg_success_rate,
            'trade_span': get_trade_type(interval)
        }

    except subprocess.CalledProcessError as e:
        logging.error(f"Subprocess failed for {interval}: {e}")
        return None
    except Exception as ex:
        logging.error(f"Error in evaluate_interval for {interval}: {ex}")
        return None

# 🚀 Parallel execution of intervals
def evaluate_all_intervals():
    results = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {executor.submit(evaluate_interval, interval): interval for interval in intervals_to_test}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
    return results

# 🧠 Pick best based on total return and SR
def select_best_interval(results):
    if not results:
        return None
    df = pd.DataFrame(results)
    df = df.sort_values(by=['success_rate', 'total_return'], ascending=False)
    return df.iloc[0]

# 💾 Write AI recommendation output
def write_ai_recommendation(best):
    output_path = os.path.join(user_data_dir, f"{user_id}_{symbol}_AI_recommend.csv")
    output_df = pd.DataFrame([{
        "Symbol": symbol,
        "AI Recommended Interval": best.get('interval'),
        "Trade Span": best['trade_span'],
        "Success Rate": f"{best['success_rate']:.2f}%",
        "Total Return": f"${best['total_return']:.2f}"
    }])
    output_df.to_csv(output_path, index=False)
    logging.info(f"✅ Final AI recommendation saved to {output_path}")
    return output_path

# 🔁 Main logic
if __name__ == "__main__":
    logging.info(f"🔍 Evaluating {symbol} for all intervals...")

    results = evaluate_all_intervals()
    if not results:
        logging.error("❌ No valid interval results to evaluate.")
        sys.exit(1)

    best = select_best_interval(results)
    if best is None:

        logging.error("❌ Could not determine best interval.")
        sys.exit(1)

    write_ai_recommendation(best)
