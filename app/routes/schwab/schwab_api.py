# /var/www/stockwicks/app/routes/schwab_api.py

from fastapi import APIRouter, HTTPException, Query
from app.utils.stock.schwab_token import get_valid_access_token
import requests

router = APIRouter()

@router.get("/schwab/quote")
def get_quote(symbol: str = Query(...)):
    access_token = get_valid_access_token()
    if not access_token:
        raise HTTPException(status_code=401, detail="Re-authentication required. Please /auth/schwab/start")
    url = f"https://api.schwabapi.com/marketdata/v1/quotes"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, params={"symbols": symbol})
    return resp.json()
