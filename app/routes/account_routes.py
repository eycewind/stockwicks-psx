# app/modules/users/account_routes.py

from fastapi import APIRouter, Depends, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.models.user import User
from app.routes.auth import get_current_user, hash_password

router = APIRouter(tags=["Account"])
templates = Jinja2Templates(directory="app/templates")


class AccountForm:
    def __init__(
        self,
        first_name: str = "",
        last_name: str = "",
        email: str = "",
        username: str = "",
        password: str = "",
        confirm_password: str = "",
    ):
        self.first_name = first_name or ""
        self.last_name = last_name or ""
        self.email = email or ""
        self.username = username or ""
        self.password = password or ""
        self.confirm_password = confirm_password or ""


@router.get("/account", response_class=HTMLResponse, name="commercial_account")
def account_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    form = AccountForm(
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        email=current_user.email,
        username=current_user.username,
    )

    success_message = request.session.pop("success_message", None)

    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": current_user,
            "form": form,
            "success_message": success_message,
            "title": "Account",
        },
    )


@router.post("/account", response_class=HTMLResponse, name="commercial_update_account")
def update_account(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = AccountForm(
        first_name=first_name,
        last_name=last_name,
        email=email,
        username=username,
        password=password,
        confirm_password=confirm_password,
    )

    if password or confirm_password:
        if password != confirm_password:
            return templates.TemplateResponse(
                "account.html",
                {
                    "request": request,
                    "user": current_user,
                    "form": form,
                    "error": "Passwords do not match.",
                    "title": "Account",
                },
                status_code=400,
            )

        current_user.password = hash_password(password)

    current_user.first_name = first_name
    current_user.last_name = last_name
    current_user.email = email
    current_user.username = username

    db.commit()

    request.session["success_message"] = "Your account was updated successfully."

    return RedirectResponse(
        url=str(request.url.path),
        status_code=status.HTTP_302_FOUND,
    )