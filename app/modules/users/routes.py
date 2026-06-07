"""
Commercial users module.

For MVP, this module wraps the cleaned production auth/user routers.
Later we can move auth logic here fully.
"""

from fastapi import APIRouter

from app.routes.auth import router as auth_router
from app.routes.user import router as user_router
from app.modules.users.account_routes import router as account_router

router = APIRouter()
router.include_router(auth_router, prefix="/auth")
router.include_router(user_router)

router.include_router(account_router)
