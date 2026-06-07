# app/utils/stock/news_api.py
import os
import requests
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def get_market_news(category: str = "general", limit: int = 5):
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        logger.error("FINNHUB_API_KEY is not set")
        return []

    url = "https://finnhub.io/api/v1/news"
    params = {"category": category, "token": api_key}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        news_items = resp.json()
        news_data = []
        for item in news_items[:limit]:
            news_data.append({
                "headline": item.get("headline"),
                "url": item.get("url"),
                "image": item.get("image", ""),
                "summary": item.get("summary", ""),
                "source": item.get("source", ""),
                "datetime": datetime.fromtimestamp(item["datetime"]).strftime('%Y-%m-%d %H:%M:%S')
                if "datetime" in item else ""
            })
        return news_data
    except Exception as e:
        logger.error(f"Error fetching market news: {e}")
        return []
