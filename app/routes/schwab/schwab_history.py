# app/routes/schwab_history.py
from fastapi import APIRouter, Query, HTTPException, Depends
from app.utils.stock.schwab_price_history import get_schwab_history, get_schwab_intraday_multi_day
from app.routes.auth import get_current_user  # Import your user dependency
from app.models.user import User

router = APIRouter()

@router.get("/schwab/history")
def schwab_history(
    symbol: str = Query(..., description="Stock symbol, e.g. AAPL"),
    periodType: str = Query("day"),
    period: int = Query(10),
    frequencyType: str = Query("minute"),
    frequency: int = Query(1),
    startDate: int = Query(None, description="Start date in ms since epoch"),
    endDate: int = Query(None, description="End date in ms since epoch"),
    needExtendedHoursData: bool = Query(False),
    needPreviousClose: bool = Query(False),
    user: User = Depends(get_current_user)
):
    """
    Get price history from Schwab API.
    - For intraday (minute) → use get_schwab_intraday_multi_day (skips weekends/holidays).
    - For daily/weekly → fallback to get_schwab_history.
    Access is restricted to paid plans only.
    """
    # Restrict to paid users only
    if not user or user.plan.startswith("free"):
        raise HTTPException(
            status_code=403,
            detail="You need an active paid plan to access Schwab market data. Please upgrade your plan."
        )

    try:
        if frequencyType == "minute":
            # Map minute frequency to interval string
            interval_map = {
                1: "1min",
                5: "5min",
                10: "10min",
                15: "15min",
                30: "30min"
            }
            interval = interval_map.get(frequency)
            if not interval:
                raise HTTPException(status_code=400, detail=f"Unsupported intraday frequency: {frequency}")

            # Use intraday fetcher (default: last N market days = period)
            df = get_schwab_intraday_multi_day(symbol, interval, num_days=period, user_id=user.id, save_file=False)
            return df.to_dict(orient="records") if not df.empty else []
        else:
            # Use Schwab passthrough for daily/weekly
            data = get_schwab_history(
                symbol=symbol,
                periodType=periodType,
                period=period,
                frequencyType=frequencyType,
                frequency=frequency,
                startDate=startDate,
                endDate=endDate,
                needExtendedHoursData=needExtendedHoursData,
                needPreviousClose=needPreviousClose
            )
            return data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
