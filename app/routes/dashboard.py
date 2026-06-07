from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.routes.auth import get_current_user

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse, name="dashboard")
def dashboard_page(request: Request, current_user=Depends(get_current_user)):
    return templates.TemplateResponse(
        "dashboard_mvp.html",
        {
            "request": request,
            "user": current_user,
            "title": "StockWicks Dashboard",
        },
    )


@router.get("/account", response_class=HTMLResponse, name="account")
def account_page(request: Request, current_user=Depends(get_current_user)):
    return templates.TemplateResponse(
        "account_mvp.html",
        {
            "request": request,
            "user": current_user,
            "title": "Account",
        },
    )
