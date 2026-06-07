# app/utils/plan_limiter.py
# app/utils/plan_limiter.py

from fastapi import HTTPException
from app.models.user import User

# ✅ Centralized feature limits by tier
BASE_PLAN_FEATURES = {
    "free": {
        "max_predictions_per_week": 15000,
        "allow_notifications": 50,
        "ai_trader": True,
        "options_query_limit": 50,
    },
    "starter": {
        "max_predictions_per_week": 15000,
        "allow_notifications": 50,
        "ai_trader": True,
        "options_query_limit": 50,
    },
    "pro": {
        "max_predictions_per_week": float("inf"),
        "allow_notifications": float("inf"),
        "ai_trader": True,
        "options_query_limit": 20,
    },
}

# ✅ Map real Stripe plan codes to base plans
PLAN_MAP = {
    "free_plan_month": "free",
    "free_plan_year": "free",
    "starter_plan_month": "starter",
    "starter_plan_year": "starter",
    "pro_plan_month": "pro",
    "pro_plan_year": "pro",
}


def check_plan(user: User, feature: str):
    # 🧠 Use stored plan or fallback
    plan_code = getattr(user, "plan", "free_plan_month")
    base_plan = PLAN_MAP.get(plan_code)

    if base_plan is None:
        raise HTTPException(status_code=400, detail=f"Unknown plan code: {plan_code}")

    features = BASE_PLAN_FEATURES.get(base_plan)
    if features is None:
        raise HTTPException(status_code=400, detail=f"Feature set not found for base plan: {base_plan}")

    if feature not in features:
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' not available in your plan.")

    if features[feature] is False:
        raise HTTPException(status_code=403, detail="Upgrade required to access this feature.")

    return features[feature]
