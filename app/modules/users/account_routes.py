# app/modules/users/account_routes.py

from fastapi import APIRouter, Depends, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.models.user import User
from app.routes.auth import get_current_user, hash_password, is_strong_password

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


def render_account(
    request: Request,
    user: User,
    form: AccountForm,
    success_message: str = None,
    error: str = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "request": request,
            "user": user,
            "form": form,
            "success_message": success_message,
            "error": error,
            "title": "Account",
        },
        status_code=status_code,
    )


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

    return render_account(
        request=request,
        user=current_user,
        form=form,
        success_message=success_message,
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

    db_user = db.query(User).filter(User.id == current_user.id).first()

    if not db_user:
        return render_account(
            request=request,
            user=current_user,
            form=form,
            error="User account not found.",
            status_code=404,
        )

    # Detect real password field used by commercial User model
    password_field = None
    for field_name in ("password", "hashed_password", "password_hash"):
        if hasattr(db_user, field_name):
            password_field = field_name
            break

    if not password_field:
        return render_account(
            request=request,
            user=current_user,
            form=form,
            error="Password column not found on User model. Check app/models/user.py.",
            status_code=500,
        )

    old_hash = getattr(db_user, password_field) or ""

    print(
        f"[ACCOUNT_UPDATE] START user_id={db_user.id} "
        f"password_field={password_field} "
        f"password_len={len(password or '')} "
        f"confirm_len={len(confirm_password or '')} "
        f"old_hash_start={old_hash[:20]}"
    )

    duplicate = (
        db.query(User)
        .filter(
            User.id != db_user.id,
            ((User.username == username) | (User.email == email)),
        )
        .first()
    )

    if duplicate:
        return render_account(
            request=request,
            user=current_user,
            form=form,
            error="Username or email is already used by another account.",
            status_code=400,
        )

    password_changed = False

    if password or confirm_password:
        if password != confirm_password:
            print(f"[ACCOUNT_UPDATE] PASSWORD_MISMATCH user_id={db_user.id}")
            return render_account(
                request=request,
                user=current_user,
                form=form,
                error="Passwords do not match.",
                status_code=400,
            )

        if not is_strong_password(password):
            print(f"[ACCOUNT_UPDATE] WEAK_PASSWORD user_id={db_user.id}")
            return render_account(
                request=request,
                user=current_user,
                form=form,
                error="Password must be at least 8 characters and include at least one uppercase letter, one lowercase letter, one digit, and one special character.",
                status_code=400,
            )

        new_hash = hash_password(password)
        setattr(db_user, password_field, new_hash)
        password_changed = True

        print(
            f"[ACCOUNT_UPDATE] PASSWORD_HASH_SET user_id={db_user.id} "
            f"password_field={password_field} "
            f"new_hash_start={new_hash[:20]}"
        )
    else:
        print(f"[ACCOUNT_UPDATE] NO_PASSWORD_SUBMITTED user_id={db_user.id}")

    db_user.first_name = first_name
    db_user.last_name = last_name
    db_user.email = email
    db_user.username = username

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    saved_hash = getattr(db_user, password_field) or ""

    print(
        f"[ACCOUNT_UPDATE] COMMITTED user_id={db_user.id} "
        f"username_after={db_user.username} "
        f"email_after={db_user.email} "
        f"password_changed={password_changed} "
        f"saved_hash_start={saved_hash[:20]}"
    )

    if password_changed:
        request.session["success_message"] = "Your account and password were updated successfully."
    else:
        request.session["success_message"] = "Your account was updated successfully."

    return RedirectResponse(
        url=str(request.url.path),
        status_code=status.HTTP_302_FOUND,
    )