# app/routes/auth.py
from fastapi import APIRouter, HTTPException, Depends, status, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from datetime import datetime, timedelta
import uuid
import bcrypt
from passlib.hash import pbkdf2_sha256 
from fastapi.responses import HTMLResponse
from app.forms.account import UpdateAccountForm

from app.models.user import User
from app.database.connection import get_db
from app.config import settings
from app.services.email_service import EmailService
from werkzeug.security import check_password_hash
from app.config import settings
import re

# ✅ Router Setup
router = APIRouter(tags=["Authentication"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
templates = Jinja2Templates(directory="app/templates")

def is_strong_password(password: str) -> bool:
    """
    Enforce at least:
        - 8 characters
        - one uppercase
        - one lowercase
        - one digit
        - one special char (@$!%*?&^#_)
    """
    pattern = r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&^#_])[A-Za-z\d@$!%*?&^#_]{8,}$'
    return re.match(pattern, password) is not None
# ✅ Hashing Functions
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

# def verify_password(plain_password: str, hashed_password: str) -> bool:
#     return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))



# ✅ Smart Verifier: Detect hash type & verify accordingly
def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        if hashed_password.startswith("pbkdf2:"):  # werkzeug hash
            return check_password_hash(hashed_password, plain_password)
        else:  # bcrypt hash
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception as e:
        print(f"Password verification failed: {e}")
        return False


# ✅ Token Management
def create_access_token(data: dict, expires_delta: timedelta = None):
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    data.update({"exp": expire})
    return jwt.encode(data, settings.secret_key, algorithm=settings.algorithm)

def generate_verification_token() -> str:
    return str(uuid.uuid4())

# ✅ Get Current User

def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token") or request.headers.get("Authorization", "").split(" ")[-1]

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username: str = payload.get("sub")
        user = db.query(User).filter(User.username == username).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")

        # ✅ Fix: assign default plan if null
        if not user.plan:
            user.plan = "free_plan_month"
            db.commit()

        return user

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ✅ Email Service Dependency
def get_email_service() -> EmailService:
    return EmailService()

# ✅ User Registration


@router.get("/register", name="register_page")
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"request": request})


@router.post("/register", status_code=status.HTTP_201_CREATED, name="register")
def register_user(
    request: Request,
    first_name: str = Form(...), 
    last_name: str = Form(...), 
    email: str = Form(...),
    username: str = Form(...), 
    password: str = Form(...), 
    accept_agreement: bool = Form(...),
    db: Session = Depends(get_db), 
    email_service: EmailService = Depends(get_email_service)
):
    # Agreement check
    if not accept_agreement:
        raise HTTPException(status_code=400, detail="You must accept the user agreement to register.")

    # Duplicate user check
    if db.query(User).filter((User.username == username) | (User.email == email)).first():
        raise HTTPException(status_code=400, detail="Username or email already registered.")

    # Password strength check
    if not is_strong_password(password):
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters and include at least one uppercase letter, one lowercase letter, one digit, and one special character."
        )

    verification_token = generate_verification_token()

    # Create user in database
    new_user = User(
        first_name=first_name,
        last_name=last_name,
        email=email,
        username=username,
        hashed_password=hash_password(password),
        is_active=True,
        is_verified=False,
        verification_token=verification_token,
        plan="mvp",
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Send email verification with commercial client URL support.
    public_base_url = getattr(settings, "PUBLIC_BASE_URL", None)
    if public_base_url:
        verification_link = f"{str(public_base_url).rstrip('/')}/auth/verify/{verification_token}"
    else:
        forwarded_prefix = request.headers.get("x-forwarded-prefix", "")
        base_url = str(request.base_url).rstrip("/")
        verification_link = f"{base_url}{forwarded_prefix}/auth/verify/{verification_token}"

    email_service.send_verification_email(new_user.email, verification_link)

    return {"message": "User registered successfully. Please check your email to verify."}

# ✅ Login Route
@router.get("/login", name="login_page")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == username).first()
    
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Invalid username or password"}
        )

    if not getattr(user, 'is_verified', True):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Email not verified. Check your email."}
        )

    # ✅ Create JWT token
    access_token = create_access_token(data={"sub": user.username})

    # ✅ Set user_id in session for AuthRedirectMiddleware
    request.session["user_id"] = user.id

    # ✅ Redirect back to requested page after login, otherwise dashboard
    redirect_to = next or request.query_params.get("next") or "/auth/dashboard"

    # Safety: only allow internal/local redirects
    if not (
        redirect_to.startswith("/")
        or redirect_to.startswith("https://www.stockwicks.com/clients/")
        or redirect_to.startswith("https://stockwicks.com/clients/")
    ):
        redirect_to = "/auth/dashboard"

    response = RedirectResponse(url=redirect_to, status_code=302)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=True,
        samesite="Lax",
        max_age=3600
    )
    return response



# ✅ Logout Route
@router.get("/logout", name="logout_page")
def logout(request: Request):
    request.session.clear()
    url_prefix = request.headers.get("x-forwarded-prefix", "")
    response = RedirectResponse(url=f"{url_prefix}/auth/login")
    response.delete_cookie("access_token")
    return response

# ✅ Recover Password Page
@router.get("/recover-password", name="recover_password_page")
def recover_password_page(request: Request):
    return templates.TemplateResponse(request, "recover_password.html", {"request": request})

# ✅ Recover Username Page
@router.get("/recover-username", name="recover_username_page")
def recover_username_page(request: Request):
    return templates.TemplateResponse(request, "recover_username.html", {"request": request})

# ✅ Process Password Recovery
@router.post("/recover-password", name="recover_password")
def process_password_recovery(
    request: Request, 
    email: str = Form(...), 
    db: Session = Depends(get_db), 
    email_service: EmailService = Depends(get_email_service)
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return templates.TemplateResponse(request, "password_reset_confirmation.html", {"request": request, "error": "No account found with that email."}
        )

    reset_token = generate_verification_token()
    user.verification_token = reset_token  
    db.commit()

    reset_link = f"{request.base_url}auth/reset-password?token={reset_token}"
    email_service.send_reset_email(user.email, reset_link)

    return templates.TemplateResponse(request, "password_reset_confirmation.html", {"request": request, "email": email}
    )



# ✅ Get Current User Route
@router.get("/get-current-user", name="get_current_user_info")
def get_current_user_info(user=Depends(get_current_user)):
    return {"user_id": user.id, "username": user.username}

# ✅ Process Username Recovery
@router.post("/recover-username", name="recover_username")
def process_username_recovery(
    request: Request, 
    email: str = Form(...), 
    db: Session = Depends(get_db), 
    email_service: EmailService = Depends(get_email_service)
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return templates.TemplateResponse(request, "username_recovery_confirmation.html", {"request": request, "error": "No account found with that email."}
        )

    # ✅ Fix: Use correct email function name
    email_service.send_username_email(user.email, user.username)

    return templates.TemplateResponse(request, "username_recovery_confirmation.html", {"request": request, "username": user.username, "email": email}
    )


# ✅ Show Reset Password Page on GET Request
@router.get("/reset-password", name="reset_password_page")
def reset_password_page(request: Request, token: str):
    return templates.TemplateResponse(request, "reset_password.html", {"request": request, "token": token})

# ✅ Process Reset Password Submission on POST Request
@router.post("/reset-password", name="reset_password")
def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)
):
    if password != confirm_password:
        return templates.TemplateResponse(request, "reset_password.html", {"request": request, "token": token, "error": "Passwords do not match."})

    user = db.query(User).filter(User.verification_token == token).first()
    if not user:
        raise HTTPException(status_code=404, detail="Invalid or expired password reset link.")

    user.hashed_password = hash_password(password)  
    user.verification_token = None  
    db.commit()

    return RedirectResponse(url=request.url_for("login_page"), status_code=302)


from sqlalchemy.orm import Session
from app.database.connection import get_db

@router.get("/notifications", name="notifications_page")
def notifications_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return templates.TemplateResponse(request, "notifications.html", {
        "request": request,
        "notifications": [],
        "user": current_user  # 👈 this is what base.html expects
    })

@router.get("/algo-backtest", name="algo_backtest_page")
def algo_backtest_page(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    return templates.TemplateResponse(request, "check_algo.html", {
        "request": request,
        "user": current_user  # ✅ This key must be 'user' to match base.html
    })


@router.get("/account", response_class=HTMLResponse, name="account")  # ✅ match this name
def account_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    form = UpdateAccountForm(
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        email=current_user.email,
        username=current_user.username,
    )

    # ✅ Grab the session success message
    success_message = request.session.pop("success_message", None)

    return templates.TemplateResponse(request, "account.html", {
        "request": request,
        "form": form,
        "user": current_user,
        "success_message": success_message  # 🔥 important!
    })


@router.post("/account", response_class=HTMLResponse)
def update_account(
    request: Request,
    form: UpdateAccountForm = Depends(UpdateAccountForm.as_form),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # ✅ Update password only if provided
    if form.password:
        hashed_password = hash_password(form.password)
        current_user.hashed_password = hashed_password

    # ✅ Update other user fields
    current_user.first_name = form.first_name
    current_user.last_name = form.last_name
    current_user.email = form.email
    current_user.username = form.username

    db.commit()

    # ✅ Set flash message in session
    request.session["success_message"] = "Your account was updated successfully."

    # ✅ Redirect to account page
    return RedirectResponse(url="/auth/account", status_code=status.HTTP_302_FOUND)

@router.get("/verify/{token}", name="verify_email")
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.verification_token == token).first()
    if not user:
        # You can show a template or just return this error.
        raise HTTPException(status_code=404, detail="Invalid or expired verification link.")
    user.is_verified = True
    user.verification_token = None
    db.commit()
    # Optionally, show a "success" template, or redirect to login.
    return RedirectResponse(url="/auth/login", status_code=302)


    # Optional-user dependency: returns User or None (no exception)
from typing import Optional
def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    try:
        return get_current_user(request, db)  # your existing function
    except HTTPException:
        return None
    except Exception:
        return None
