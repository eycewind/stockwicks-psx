# utils/schwab_options.py
import requests
from app.utils.stock.schwab_token import get_valid_access_token  # FIXED import

def get_options_chain(symbol: str, expiry: str) -> dict:
    """
    Fetches the options chain for a given symbol and expiration date.
    Expiry format: YYYY-MM-DD
    """
    token = get_valid_access_token()  # FIXED usage
    headers = {"Authorization": f"Bearer {token}"}

    url = "https://api.schwabapi.com/marketdata/v1/chains"
    params = {
        "symbol": symbol.upper(),
        "contractType": "ALL",
        "strategy": "SINGLE",
        "fromDate": expiry,
        "toDate": expiry
    }

    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

def extract_oi_data(option_chain: dict):
    """
    Parses call/put open interest from Schwab options chain response.
    Returns a list of dicts with strike, type (call/put), and open interest.
    """
    result = []

    for option_type in ["callExpDateMap", "putExpDateMap"]:
        is_call = option_type == "callExpDateMap"
        date_map = option_chain.get(option_type, {})

        for expiry_key, strikes in date_map.items():
            for strike_price, contracts in strikes.items():
                if not contracts:
                    continue
                contract = contracts[0]  # usually just one
                result.append({
                    "strike": float(strike_price),
                    "type": "call" if is_call else "put",
                    "open_interest": contract.get("openInterest", 0),
                    "symbol": contract.get("symbol"),
                    "bid": contract.get("bid"),
                    "ask": contract.get("ask")
                })

    return result
