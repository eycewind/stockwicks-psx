from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.base import Base


class SparkieJob(Base):
    __tablename__ = "sparkie_jobs"

    id = Column(String(80), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    status = Column(String(40), default="queued", index=True, nullable=False)
    stage = Column(String(60), default="queued", nullable=False)
    message = Column(Text, nullable=True)

    account_equity = Column(Float, nullable=False)
    target_profit = Column(Float, nullable=False)
    target_period = Column(String(20), default="daily", nullable=False)
    confidence_level = Column(Float, default=0.60, nullable=False)

    symbol_bucket_json = Column(Text, nullable=True)
    interval_policy_json = Column(Text, nullable=True)
    algo_policy_json = Column(Text, nullable=True)
    request_json = Column(Text, nullable=True)

    progress_pct = Column(Float, default=0.0, nullable=False)
    elapsed_seconds = Column(Integer, default=0, nullable=False)
    eta_seconds = Column(Integer, nullable=True)
    total_steps = Column(Integer, default=0, nullable=False)
    completed_steps = Column(Integer, default=0, nullable=False)

    candidates_tested = Column(Integer, default=0, nullable=False)
    best_symbol = Column(String(50), nullable=True)
    best_interval = Column(String(30), nullable=True)
    best_algo_name = Column(String(100), nullable=True)
    best_score = Column(Float, nullable=True)
    best_profit_loss = Column(Float, nullable=True)
    best_trades = Column(Integer, nullable=True)
    best_win_rate = Column(Float, nullable=True)
    recommendation = Column(String(40), nullable=True)
    decision_reason = Column(Text, nullable=True)

    replay_session_id = Column(Integer, nullable=True)
    task_id = Column(String(120), nullable=True)
    error_message = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    stop_requested_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SparkieCandidate(Base):
    __tablename__ = "sparkie_candidates"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(80), ForeignKey("sparkie_jobs.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    symbol = Column(String(50), index=True, nullable=False)
    interval = Column(String(30), nullable=False)
    algo_name = Column(String(100), nullable=False)
    status = Column(String(40), default="tested", nullable=False)

    score = Column(Float, nullable=True)
    profit_loss = Column(Float, nullable=True)
    trades = Column(Integer, nullable=True)
    win_rate = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    validation_profit_loss = Column(Float, nullable=True)
    validation_trades = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=True)
    params_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SparkieEvent(Base):
    __tablename__ = "sparkie_events"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(80), ForeignKey("sparkie_jobs.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    level = Column(String(20), default="info", nullable=False)
    stage = Column(String(60), nullable=True)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SparkieWeeklyRun(Base):
    __tablename__ = "sparkie_weekly_runs"

    id = Column(String(80), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    status = Column(String(40), default="queued", index=True, nullable=False)
    stage = Column(String(60), default="queued", nullable=False)
    message = Column(Text, nullable=True)
    run_mode = Column(String(20), default="manual", nullable=False)

    baseline_cash = Column(Float, default=10000.0, nullable=False)
    batch_size = Column(Integer, default=5, nullable=False)
    total_symbols = Column(Integer, default=0, nullable=False)
    completed_symbols = Column(Integer, default=0, nullable=False)
    failed_symbols = Column(Integer, default=0, nullable=False)
    current_batch = Column(Integer, default=0, nullable=False)
    current_symbol = Column(String(50), nullable=True)
    progress_pct = Column(Float, default=0.0, nullable=False)
    elapsed_seconds = Column(Integer, default=0, nullable=False)
    eta_seconds = Column(Integer, nullable=True)

    universe_json = Column(Text, nullable=True)
    intervals_json = Column(Text, nullable=True)
    algos_json = Column(Text, nullable=True)
    config_hash = Column(String(64), index=True, nullable=True)
    task_id = Column(String(120), nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    stop_requested_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    summary_json = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SparkieWeeklyResult(Base):
    __tablename__ = "sparkie_weekly_results"
    __table_args__ = (
        UniqueConstraint("run_id", "symbol", "interval", "algo_name", name="uq_sparkie_weekly_result_combo"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(String(80), ForeignKey("sparkie_weekly_runs.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(50), index=True, nullable=False)
    universe_rank = Column(Integer, nullable=True)
    weighted_alpha = Column(Float, nullable=True)
    interval = Column(String(30), nullable=False)
    algo_name = Column(String(100), nullable=False)

    reference_price = Column(Float, nullable=True)
    baseline_cash = Column(Float, nullable=False)
    baseline_shares = Column(Integer, nullable=False)
    score = Column(Float, nullable=True)
    average_daily_pnl = Column(Float, nullable=True)
    median_daily_pnl = Column(Float, nullable=True)
    profitable_day_rate = Column(Float, nullable=True)
    max_daily_profit = Column(Float, nullable=True)
    max_daily_loss = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    trades = Column(Integer, nullable=True)
    win_rate = Column(Float, nullable=True)
    validation_profit_loss = Column(Float, nullable=True)
    validation_trades = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=True)
    daily_pnl_json = Column(Text, nullable=True)
    params_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SparkieWeeklySchedule(Base):
    __tablename__ = "sparkie_weekly_schedules"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    weekday = Column(Integer, default=6, nullable=False)
    hour_et = Column(Integer, default=2, nullable=False)
    last_trigger_date = Column(String(10), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
