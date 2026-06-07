# data_fetching.py
import requests
import logging
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(level=logging.INFO)

def get_stock_data(symbol, interval, api_token, days):
    """
    Fetch stock data from the API for any given interval and time span specified by days.

    Parameters:
        symbol (str): Stock symbol.
        interval (str): Data interval as required by the API (e.g., '1min', '5min', '15min', '60min', '1D').
        api_token (str): API token for authentication.
        days (int): Number of days of data to fetch.

    Returns:
        dict: Parsed JSON response with stock data or None if an error occurs.
    """
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days)
    start_date_str = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_date_str = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')

    url = f"https://api.marketdata.app/v1/stocks/candles/{interval}/{symbol}?from={start_date_str}&to={end_date_str}&token={api_token}"
    response = requests.get(url, headers={'Accept': 'application/json'})
    
    if response.status_code == 200:
        logging.info(f"Data successfully fetched for {symbol} at interval {interval} over {days} days")
        return response.json()
    else:
        logging.error(f"Error fetching data for {symbol} at interval {interval} over {days} days: {response.status_code} {response.text}")
        return None
