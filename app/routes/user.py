# app/routes/user.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from app.routes.auth import get_current_user  # ✅ Correct Import
from app.database.connection import get_db
from app.models import User  # ✅ Fixed Import (Avoid Circular Import)

router = APIRouter(prefix="/users", tags=["Users"])  # ✅ Added Prefix & Tags


# ✅ Pydantic Model for User Response
class UserResponse(BaseModel):
    id: int
    username: str
    email: str

    class Config:
        from_attributes = True


@router.get("/", response_model=list[UserResponse])
def read_users(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """🔒 Returns a list of all users (Admin Only)"""

    # ✅ Ensure only admin users can fetch all users
    if not user or not hasattr(user, "is_admin") or not user.is_admin:
        raise HTTPException(status_code=403, detail="🚫 Access Denied: Admins only")

    users = db.query(User).all()
    return users


@router.get("/me", response_model=UserResponse)
def get_my_profile(user: User = Depends(get_current_user)):
    """✅ Returns the logged-in user's details"""
    return user

### user plan
@router.get("/auth/my-plan")
async def get_user_plan(
    db: Session = Depends(get_db),
    user=Depends(get_current_user)
):
    return JSONResponse(content={
        "plan": user.plan,
        "prediction_count": user.prediction_count,
        "last_reset": str(user.last_prediction_reset),
    })