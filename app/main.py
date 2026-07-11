#main.py
# /var/www/stockwicks/app/main.py
# ==========================
# Standard Library
# ==========================
import sys
import os
import logging
import urllib.parse

# ==========================
# Third-Party Libraries
# ==========================
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route, Mount
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

# DB / SQLAlchemy exceptions (for graceful DB-busy handling)
from sqlalchemy.exc import TimeoutError as SATimeoutError, OperationalError as SAOperationalError
import psycopg2

# ==========================
# Internal App Imports
# ==========================
from app.config import settings
from app.database.connection import get_db, SessionLocal
from app.models.user import User

# Core routes
from app.routes.auth import get_current_user
from app.modules.users.routes import router as users_router
from app.routes.pages import router as pages_router
from app.modules.dashboard.routes import router as dashboard_router
from app.modules.broker.routes import router as broker_router, legacy_router as broker_legacy_router
from app.modules.replay.routes import router as replay_router
from app.routes import log_analysis
from app.routes import admin_live_trades
from app.routes import spx_0dte_routes, spx_0dte_trades

# Schwab routes
from app.routes.schwab_trade import (
    ui_router as schwab_ui_router,
    trade_router as schwab_trade_router,
)


# Other core routes
from app.routes.schwab import (
    schwab_auth,
    schwab_history,
    schwab_api,
)

# Ensure app imports resolve
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.env"))
logger.info(f"Loading .env from: {env_path}")
load_dotenv(env_path)

# App
app = FastAPI(title="StockWicks API")
app.state.client_slug = settings.client_slug

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://stonxs.com",
        "https://www.stonxs.com",
        "https://stockwicks.com",
        "https://www.stockwicks.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sessions
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.access_token_expire_minutes * 60,
)

# -------------------------------------------------------------------
# Request logging
# IMPORTANT FIX:
# - Do NOT call next(get_db()) here (it can leak sessions under load)
# - Use SessionLocal() and ALWAYS close it
# -------------------------------------------------------------------
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        user_id = "anonymous"
        username = "anonymous"

        token = request.cookies.get("access_token")
        if token:
            try:
                payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
                username = payload.get("sub")

                if username:
                    # Look up DB id once (safe: always close)
                    try:
                        db = SessionLocal()
                        try:
                            user = db.query(User).filter(User.username == username).first()
                            if user:
                                user_id = user.id
                                username = user.username
                        finally:
                            try:
                                db.close()
                            except Exception:
                                pass
                    except Exception as db_exc:
                        logger.error(f"DB lookup failed in RequestLoggingMiddleware: {db_exc}")

            except JWTError:
                username = "invalid_token"

        response = await call_next(request)
        logger.info(
            f"UserID={user_id} | Username={username} | IP={request.client.host} | {request.method} {request.url.path} → {response.status_code}"
        )
        return response


app.add_middleware(RequestLoggingMiddleware)

# Auth redirect middleware
class AuthRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        protected_paths = (
            "/auth/dashboard",
            "/auth/papertradebot",
            "/auth/replay",
            "/auth/schwab",
            "/auth/trade",
            "/broker",
            "/trade",
            "/replay-simulator",
            "/analysis",
            "/account",
            "/admin",
        )
        path = request.url.path

        # Schwab OAuth callbacks must not be intercepted by auth middleware.
        # The broker callback validates oauth session/state itself.
        schwab_callback_paths = (
            "/auth/schwab/callback",
            "/auth/schwab/db/callback",
            "/broker/schwab/callback",
        )
        if path in schwab_callback_paths:
            return await call_next(request)

        if any(path.startswith(p) for p in protected_paths):
            token = request.cookies.get("access_token")
            forwarded_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
            login_url = f"{forwarded_prefix}/auth/login" if forwarded_prefix else "/auth/login"
            if not token:
                return RedirectResponse(url=login_url)
            try:
                jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
            except JWTError:
                return RedirectResponse(url=login_url)
        return await call_next(request)

app.add_middleware(AuthRedirectMiddleware)

# Templates (global)
templates = Jinja2Templates(directory="app/templates")

# Static/data
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))

if not os.path.exists(STATIC_DIR):
    raise RuntimeError(f"❌ Static directory not found: {STATIC_DIR}")
if not os.path.exists(DATA_DIR):
    logger.warning(f"⚠️ Data directory not found: {DATA_DIR}")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/data", StaticFiles(directory=DATA_DIR, check_dir=True), name="data")
logger.info(f"✅ Serving static files from: {STATIC_DIR}")
logger.info(f"✅ Serving data files from: {DATA_DIR}")

@app.get("/data/{user_id}/{filename}")
async def serve_data_file(user_id: str, filename: str):
    file_path = os.path.join(DATA_DIR, user_id, filename)
    logger.info(f"Attempting to serve file: {file_path}")
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        raise HTTPException(status_code=404, detail=f"File {filename} not found for user {user_id}")
    return FileResponse(file_path, media_type="application/json")


# Register routers
from app.routes import paper_trade_bot
app.include_router(users_router)
app.include_router(pages_router)
app.include_router(dashboard_router, prefix="/auth")
app.include_router(broker_router)
app.include_router(broker_legacy_router)
app.include_router(paper_trade_bot.router)
app.include_router(replay_router)
app.include_router(log_analysis.router)
app.include_router(admin_live_trades.router)
app.include_router(spx_0dte_routes.router)
app.include_router(spx_0dte_trades.router)
# External integrations
# Schwab trade
app.include_router(schwab_trade_router)  # has prefix="/trade" internally
app.include_router(schwab_ui_router, prefix="/auth")     # registers "/trade/ui" internally
app.include_router(schwab_auth.router)
app.include_router(schwab_history.router)
app.include_router(schwab_api.router, prefix="/auth")




# -------------------------
# Startup
# -------------------------
@app.on_event("startup")
async def startup_event():
    logger.info("Starting background tasks...")
    try:
        from app.services.spx_0dte_schema import ensure_spx_0dte_tables

        ensure_spx_0dte_tables()
    except Exception as exc:
        logger.warning("SPX 0DTE schema bootstrap skipped/failed: %s", exc)

# -------------------------
# Exception Handlers
# -------------------------
def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    xrw = request.headers.get("x-requested-with", "")
    return (
        request.url.path.startswith("/api")
        or "application/json" in accept
        or xrw.lower() == "xmlhttprequest"
    )


def _client_prefixed_url(request, path=None):
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    base_url = str(request.base_url).rstrip("/")

    target_path = path if path is not None else request.url.path
    if not target_path.startswith("/"):
        target_path = "/" + target_path

    if forwarded_prefix and not target_path.startswith(forwarded_prefix + "/"):
        target_path = forwarded_prefix + target_path

    url = base_url + target_path
    if path is None and request.url.query:
        url += "?" + request.url.query
    return url


def _client_login_redirect(request, error=None):
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    login_path = "/auth/login"
    if forwarded_prefix:
        login_path = f"{forwarded_prefix}{login_path}"

    next_url = urllib.parse.quote(_client_prefixed_url(request), safe="")
    login_url = f"{login_path}?next={next_url}"
    if error:
        login_url += f"&error={urllib.parse.quote(error)}"

    return RedirectResponse(login_url, status_code=303)


# Graceful DB busy/unavailable handling (instead of 500 Internal Server Error)
class DBUnavailable(Exception):
    """Raised when DB pool is exhausted or DB is temporarily unreachable."""
    pass

@app.exception_handler(DBUnavailable)
async def db_unavailable_handler(request: Request, exc: DBUnavailable):
    if _wants_json(request):
        return JSONResponse(
            status_code=503,
            content={"detail": "db_busy", "message": "Database is busy/unavailable. Please retry."},
        )
    # Browser page load: send to login with a friendly flag
    return _client_login_redirect(request, error="db_busy")

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code in (401, 403):
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail or "auth_required", "redirect": request.headers.get("x-forwarded-prefix", "").rstrip("/") + "/auth/login"}, status_code=401)
        if request.url.path.startswith("/auth/login"):
            return JSONResponse(status_code=401, content={"detail": "Please log in."})
        return _client_login_redirect(request)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(ExpiredSignatureError)
async def expired_token_handler(request: Request, exc: ExpiredSignatureError):
    if _wants_json(request):
        return JSONResponse({"detail": "token_expired", "redirect": request.headers.get("x-forwarded-prefix", "").rstrip("/") + "/auth/login"}, status_code=401)
    return _client_login_redirect(request)

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # Convert DB pool timeouts / DB disconnects into a graceful UX (login redirect or 503 JSON)
    if isinstance(exc, (SATimeoutError, SAOperationalError, psycopg2.OperationalError)):
        return await db_unavailable_handler(request, DBUnavailable())

    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# -------------------------
# Debug: List All Routes
# -------------------------
@app.get("/debug-routes")
async def debug_routes():
    routes_info = []
    for r in app.router.routes:
        if isinstance(r, Route):
            routes_info.append({
                "path": r.path,
                "name": r.name,
                "methods": list(r.methods),
            })
        elif isinstance(r, Mount):
            routes_info.append({
                "path": r.path,
                "name": r.name,
                "mounted_app": getattr(r.app, "__class__", type(r.app)).__name__,
            })
        else:
            routes_info.append({
                "path": getattr(r, "path", None),
                "name": getattr(r, "name", None),
                "type": r.__class__.__name__,
            })
    return routes_info

# Root health
@app.get("/healthz")
def health():
    return {"ok": True}

#AI Agent
