from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # Client/app
    client_slug: str = Field(default="template", env="CLIENT_SLUG")
    app_env: str = Field(default="development", env="APP_ENV")
    app_port: int = Field(default=8101, env="APP_PORT")
    public_base_url: str = Field(default="http://127.0.0.1:8101", env="PUBLIC_BASE_URL")
    reduced_local_runtime: bool = Field(default=False, env="REDUCED_LOCAL_RUNTIME")

    # Security
    secret_key: str = Field(default="change_me", env="SECRET_KEY")
    algorithm: str = Field(default="HS256", env="ALGORITHM")
    access_token_expire_minutes: int = Field(default=1440, env="ACCESS_TOKEN_EXPIRE_MINUTES")
    master_admin_api_key: str = Field(default="change_me", env="MASTER_ADMIN_API_KEY")

    # Database
    database_url: str = Field(
        default="postgresql://stockwicks_user:password@localhost:5432/stockwicks",
        env="DATABASE_URL",
    )

    # Redis / Celery
    redis_url: str = Field(default="redis://127.0.0.1:6379/1", env="REDIS_URL")
    celery_broker_url: str = Field(default="redis://127.0.0.1:6379/1", env="CELERY_BROKER_URL")
    celery_result_backend: str = Field(default="redis://127.0.0.1:6379/1", env="CELERY_RESULT_BACKEND")

    # Per-client folders
    data_dir: str = Field(default="data", env="DATA_DIR")
    log_dir: str = Field(default="logs", env="LOG_DIR")
    model_dir: str = Field(default="models", env="MODEL_DIR")

    # Schwab OAuth
    schwab_client_id: str = Field(default="", env="SCHWAB_CLIENT_ID")
    schwab_client_secret: str = Field(default="", env="SCHWAB_CLIENT_SECRET")
    schwab_redirect_uri: str = Field(default="", env="SCHWAB_REDIRECT_URI")

    # Trading safety
    trading_enabled: bool = Field(default=False, env="TRADING_ENABLED")
    paper_trading_enabled: bool = Field(default=True, env="PAPER_TRADING_ENABLED")
    live_trading_enabled: bool = Field(default=False, env="LIVE_TRADING_ENABLED")
    emergency_stop: bool = Field(default=False, env="EMERGENCY_STOP")
    max_daily_loss: float = Field(default=0.0, env="MAX_DAILY_LOSS")
    max_position_size: float = Field(default=0.0, env="MAX_POSITION_SIZE")

    # Email alerts — optional for MVP boot
    mail_server: str = Field(default="", env="MAIL_SERVER")
    mail_port: int = Field(default=587, env="MAIL_PORT")
    mail_username: str = Field(default="", env="MAIL_USERNAME")
    mail_password: str = Field(default="", env="MAIL_PASSWORD")
    mail_from: str = Field(default="alerts@stockwicks.com", env="MAIL_FROM")
    mail_use_tls: bool = Field(default=True, env="MAIL_USE_TLS")

    # Backward-compatible aliases used by old cleaned code.
    stock_data_api_token: str = Field(default="", env="STOCK_DATA_API_TOKEN")
    api_token: str = Field(default="", alias="API_TOKEN")
    API_TOKEN: str = Field(default="", env="API_TOKEN")
    DATA_DIR: str = Field(default="data", env="DATA_DIR")
    finnhub_api_key: str = Field(default="", env="FINNHUB_API_KEY")
    domain_url: str = Field(default="", env="DOMAIN_URL")
    sendgrid_api_key: str = Field(default="", env="SENDGRID_API_KEY")
    email_from: str = Field(default="alerts@stockwicks.com", env="EMAIL_FROM")


settings = Settings()
