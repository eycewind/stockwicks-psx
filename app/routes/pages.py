from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["Pages"])


@router.get("/", name="home")
@router.get("/", name="home_page")
def home():
    return RedirectResponse(url="/auth/login", status_code=303)


@router.get("/healthz", name="healthz")
def healthz():
    return {"ok": True, "service": "stockwicks-commercial-client"}
