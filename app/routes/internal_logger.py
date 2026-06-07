# app/routes/internal_logger.py

# app/routes/internal_logger.py

from fastapi import APIRouter, Form, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database.connection import get_db
from app.models.user import User
from app.routes.dashboard import (
    get_analysis,
    get_daytrade_analysis,
    zero_dte_friday_post
)
import logging
import pandas as pd
from app.routes.zero_dte import read_predictions, analyze_predictions, list_previous_runs
import os
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["Internal Logger"])

def get_user_116(db: Session) -> User:
    user = db.query(User).filter(User.id == 116).first()
    if not user:
        raise HTTPException(status_code=404, detail="User 116 not found")
    return user

@router.get("/get-analysis")
def run_internal_analysis(symbol: str, db: Session = Depends(get_db)):
    try:
        user = get_user_116(db)
        return get_analysis(symbol=symbol, user=user)
    except Exception as e:
        logger.error(f"Analysis failed for {symbol}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Analysis failed for {symbol}: {str(e)}")

@router.get("/get-daytrade-analysis")
def run_internal_daytrade(symbol: str, db: Session = Depends(get_db)):
    try:
        import json
        symbol = symbol.upper()
        user = get_user_116(db)
        user_id = str(user.id)

        analysis_path = f"/var/www/stockwicks/data/{user_id}/{symbol}_daytrade_analysis.json"
        data_path = f"/var/www/stockwicks/data/{user_id}/{symbol}_5min.json"

        if not os.path.exists(analysis_path):
            raise HTTPException(status_code=404, detail="Daytrade analysis not ready.")

        with open(analysis_path, "r") as f:
            analysis_data = json.load(f)

        candlestick_data = []
        if os.path.exists(data_path):
            with open(data_path, "r") as f:
                candlestick_data = json.load(f)

        return {
            "symbol": symbol,
            "analysis_data": analysis_data,
            "candlestick_data": candlestick_data
        }

    except Exception as e:
        logger.error(f"Daytrade Analysis failed for {symbol}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Daytrade Analysis failed for {symbol}: {str(e)}")

@router.post("/zero-dte-pick")
def run_internal_0dte(symbol: str = Form(...), db: Session = Depends(get_db)):
    try:
        runs = list_previous_runs(symbol)
        if not runs:
            return {"message": "No previous runs found."}

        latest_file = runs[0][0]
        df = read_predictions(latest_file)
        analysis = analyze_predictions(df) if not df.empty else []

        return {
            "symbol": symbol,
            "latest_run": latest_file,
            "predictions": df.to_dict(orient="records"),
            "analysis": analysis
        }
    except Exception as e:
        logger.error(f"0DTE Pick failed for {symbol}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"0DTE Pick failed for {symbol}: {str(e)}")

@router.post("/zero-dte-friday")
def run_internal_friday(symbol: str = Form(...)):
    try:
        from datetime import datetime
        import pandas as pd

        symbol = symbol.upper()
        log_dir = "/var/www/stockwicks/logs"
        files = [f for f in os.listdir(log_dir) if f.startswith("nearest_friday_") and f.endswith(f"{symbol}.csv")]

        if not files:
            raise HTTPException(status_code=404, detail=f"No recent 0DTE Friday file for {symbol}")

        latest_file = max(files, key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
        result_path = os.path.join(log_dir, latest_file)
        df = pd.read_csv(result_path)

        return {
            "symbol": symbol,
            "latest_file": latest_file,
            "recommendations": df.to_dict(orient="records")
        }

    except Exception as e:
        logger.error(f"0DTE Friday failed for {symbol}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"0DTE Friday failed for {symbol}: {str(e)}")
