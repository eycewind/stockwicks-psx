# /var/www/stockwicks/app/scripts/schwab_test.py
import sys
import os

# Add the parent directory to sys.path so Python finds schwabdev/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from schwabdev.client import Client

APP_KEY = "v99zYt7xLUTWcBXLfPgwsYOKXAgGEllN"
APP_SECRET = "Z7CFceh6PpREoH1Q"
CALLBACK_URL = "https://www.stockwicks.com/clients/ashakil/auth/callback"
TOKENS_FILE = "/var/www/stockwicks/data/schwab_token.json"

client = Client(
    app_key=APP_KEY,
    app_secret=APP_SECRET,
    callback_url=CALLBACK_URL,
    tokens_file=TOKENS_FILE
)

# Test: Print linked Schwab accounts
response = client.account_linked()
print("Linked Accounts:", response.json())

# Test: Print account details for all accounts
response = client.account_details_all()
print("Account Details:", response.json())

# Test: Print real-time quote for AAPL
response = client.quotes("AAPL")
print("AAPL Quote:", response.json())
