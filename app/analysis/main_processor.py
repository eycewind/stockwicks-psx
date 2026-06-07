# main_processor.py
from data_fetching import get_stock_data
from indicators import compute_rsi, compute_macd, compute_moving_averages, compute_emas, compute_atr, compute_obv, compute_atr_trailing_stops
import pandas as pd
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

def process_stock(symbol, interval, days):
    """
    Process stock data for a given symbol, interval, and number of days.

    Parameters:
        symbol (str): Stock symbol.
        interval (str): Data interval as required by the API.
        days (int): Number of days of data to fetch.

    Returns:
        DataFrame: Processed stock data with indicators and signals.
    """
    api_token = os.getenv('STOCK_DATA_API_TOKEN')
    if not api_token:
        print("API token is not set. Please check your .env file.")
        return None

    # Fetch data
    data = get_stock_data(symbol, interval, api_token, days)
    if data is None or 's' not in data or data['s'] != 'ok':
        print(f"No data available for {symbol} at {interval} over {days} days.")
        return None
    
    # Convert data to DataFrame
    df = pd.DataFrame({
        'timestamp': pd.to_datetime(data['t'], unit='s'),
        'open': data['o'],
        'high': data['h'],
        'low': data['l'],
        'close': data['c'],
        'volume': data['v']
    })
    df.set_index('timestamp', inplace=True)

    # Compute indicators
    df = compute_rsi(df)
    df = compute_macd(df)
    df = compute_moving_averages(df, [10, 50])
    df = compute_emas(df, [12, 26])
    df = compute_atr(df)
    df = compute_obv(df)
    df = compute_atr_trailing_stops(df)

    # Additional processing can be added here

    return df

# Example usage
if __name__ == '__main__':
    symbol = 'AAPL'  # Example symbol
    interval = '5min'  # Example interval
    days = 7  # Number of days to fetch data for
    processed_data = process_stock(symbol, interval, days)
    print(processed_data.head())  # Display the first few rows of the processed data
