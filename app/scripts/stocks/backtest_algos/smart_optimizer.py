#!/usr/bin/env python3
# /var/www/stockwicks/app/scripts/stock_algos/smart_optimizer.py

import numpy as np
import pandas as pd
from scipy.optimize import minimize
import subprocess
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SmartOptimizer")

def objective_function(params, symbols_file, interval):
    """Objective function to maximize total profit"""
    long_ent, long_ex, short_ent, short_ex = params
    
    # Ensure valid thresholds
    if not (0.5 <= long_ent <= 0.8 and 0.4 <= long_ex < long_ent and 
            0.2 <= short_ent <= 0.5 and short_ent < short_ex <= 0.6):
        return -999999  # Penalize invalid combinations
    
    cmd = [
        "python3", "batch_symbol_tester.py",
        "--symbols-file", symbols_file,
        "--interval", interval,
        "--long-threshold", str(long_ent),
        "--long-exit-threshold", str(long_ex),
        "--short-threshold", str(short_ent),
        "--short-exit-threshold", str(short_ex),
        "--output", "/tmp/temp_results.csv"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            df = pd.read_csv("/tmp/temp_results.csv")
            return -df['Total_Profit($)'].sum()  # Negative because we minimize
    except:
        pass
    
    return -999999

def run_smart_optimization(symbols_file, interval):
    """Run smart optimization using scipy"""
    
    # Initial guess
    x0 = [0.60, 0.55, 0.40, 0.45]
    
    # Bounds for parameters
    bounds = [
        (0.55, 0.75),  # long_entry
        (0.50, 0.65),  # long_exit  
        (0.30, 0.45),  # short_entry
        (0.40, 0.55)   # short_exit
    ]
    
    # Constraints
    constraints = [
        {'type': 'ineq', 'fun': lambda x: x[0] - x[1] - 0.05},  # long_ent - long_ex >= 0.05
        {'type': 'ineq', 'fun': lambda x: x[3] - x[2] - 0.05},  # short_ex - short_ent >= 0.05
    ]
    
    result = minimize(
        objective_function, x0, 
        args=(symbols_file, interval),
        bounds=bounds,
        constraints=constraints,
        method='SLSQP',
        options={'maxiter': 10}  # Limit iterations due to time
    )
    
    return result