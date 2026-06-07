# /var/www/stockwicks/app/scripts/options/options_data.py

import requests
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict

# --- Project Setup ---
import sys, os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
if PROJECT_ROOT not in sys.path: sys.path.insert(0, PROJECT_ROOT)

from app.utils.stock.schwab_token import get_valid_access_token

SCHWAB_API_URL = "https://api.schwabapi.com/marketdata/v1"
log = logging.getLogger(__name__)

# --- THIS IS THE CORRECTED FUNCTION ---
# This version is based on the working logic from your guru_dashboard.py script.
def fetch_option_chain(symbol: str, from_date: Optional[str] = None, to_date: Optional[str] = None) -> Optional[Dict]:
    """
    Fetches the full option chain using a date range. This is a robust, single API call.
    """
    url = f"{SCHWAB_API_URL}/chains"
    
    # Use provided dates or default to a 45-day window from today
    final_from_date = from_date if from_date else datetime.now().strftime("%Y-%m-%d")
    final_to_date = to_date if to_date else (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")

    params = {
        "symbol": symbol.upper(),
        "fromDate": final_from_date,
        "toDate": final_to_date,
        "includeUnderlyingQuote": "true", # Important for getting a reliable price
        "strategy": "SINGLE",
        "range": "ALL"
    }
    
    try:
        access_token = get_valid_access_token()
        if not access_token:
            log.error("Failed to get Schwab access token for option chain.")
            return None
            
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        
        log.info(f"Option Chain API [{symbol}] status: {resp.status_code}")
        if resp.status_code != 200:
            log.error(f"Schwab API Error for {symbol} chain: {resp.status_code} - {resp.text}")
            return None
            
        data = resp.json()
        
        # Check if the response contains actual option data and was successful
        if data.get("status") == "SUCCESS" and (data.get('callExpDateMap') or data.get('putExpDateMap')):
            return data
        else:
            log.warning(f"Schwab API returned success but no option data for {symbol}.")
            return None

    except requests.exceptions.RequestException as e:
        log.error(f"API request for option chain failed for {symbol}: {e}")
        return None
    except Exception as e:
        log.error(f"An unexpected error occurred in fetch_option_chain for {symbol}: {e}", exc_info=True)
        return None