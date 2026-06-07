#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/quick_test.py

import subprocess
import sys

# Test these specific combinations
TEST_COMBINATIONS = [
    # Conservative
    {"long_ent": 0.65, "long_ex": 0.60, "short_ent": 0.35, "short_ex": 0.40},
    # Moderate  
    {"long_ent": 0.60, "long_ex": 0.55, "short_ent": 0.40, "short_ex": 0.45},
    # Aggressive
    {"long_ent": 0.55, "long_ex": 0.50, "short_ent": 0.45, "short_ex": 0.50},
    # Asymmetric
    {"long_ent": 0.62, "long_ex": 0.57, "short_ent": 0.38, "short_ex": 0.43},
]

def run_quick_test(symbols_file, interval):
    for i, combo in enumerate(TEST_COMBINATIONS, 1):
        print(f"\n🧪 Testing Combination {i}/4")
        print(f"   Long: Entry {combo['long_ent']}, Exit {combo['long_ex']}")
        print(f"   Short: Entry {combo['short_ent']}, Exit {combo['short_ex']}")
        
        cmd = [
            "python3", "batch_symbol_tester.py",
            "--symbols-file", symbols_file,
            "--interval", interval,
            "--long-threshold", str(combo['long_ent']),
            "--long-exit-threshold", str(combo['long_ex']),
            "--short-threshold", str(combo['short_ent']),
            "--short-exit-threshold", str(combo['short_ex']),
            "--output", f"quick_test_{i}.csv"
        ]
        
        subprocess.run(cmd)

if __name__ == "__main__":
    run_quick_test("symbols.txt", "5min")