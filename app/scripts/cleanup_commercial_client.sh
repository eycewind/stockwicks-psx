#!/usr/bin/env bash
set -euo pipefail

# Cleanup StockWicks commercial client code for the ashakil service surface.
#
# Keeps:
#   Dashboard, Trade Bot, Broker Setup, Replay, Log Analysis,
#   Account, Register, Login, Schwab support, and Celery schedules.
#
# Archives unwanted files instead of deleting them.
#
# Usage:
#   bash scripts/cleanup_commercial_client.sh --dry-run
#   bash scripts/cleanup_commercial_client.sh
#   bash scripts/cleanup_commercial_client.sh --skip-git-backup

DRY_RUN=0
SKIP_GIT_BACKUP=0
for arg in "$@"; do
  case "${arg}" in
    --dry-run)
      DRY_RUN=1
      ;;
    --skip-git-backup)
      SKIP_GIT_BACKUP=1
      ;;
    *)
      printf '[cleanup] ERROR: unknown argument: %s\n' "${arg}" >&2
      exit 1
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_DIR="${ROOT}/_cleanup_archive_${TS}"

log() {
  printf '[cleanup] %s\n' "$*"
}

run() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '[dry-run] %q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

archive_path() {
  local rel="$1"
  local src="${ROOT}/${rel}"
  local dst="${ARCHIVE_DIR}/${rel}"

  if [[ ! -e "${src}" ]]; then
    return 0
  fi

  log "archive ${rel}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "would move ${src} -> ${dst}"
    return 0
  fi

  mkdir -p "$(dirname "${dst}")"
  mv "${src}" "${dst}"
}

patch_file() {
  local rel="$1"
  local code="$2"
  local path="${ROOT}/${rel}"

  if [[ ! -f "${path}" ]]; then
    log "skip missing ${rel}"
    return 0
  fi

  log "patch ${rel}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    return 0
  fi

  TARGET_FILE="${path}" python - <<PY
from pathlib import Path
import os

path = Path(os.environ["TARGET_FILE"])
text = path.read_text(encoding="utf-8-sig")
${code}
path.write_text(text, encoding="utf-8")
PY
}

log "root: ${ROOT}"
if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry run only; no files will be changed"
else
  if [[ "${SKIP_GIT_BACKUP}" != "1" ]]; then
    if [[ -x "${ROOT}/scripts/git_backup_before_cleanup.sh" || -f "${ROOT}/scripts/git_backup_before_cleanup.sh" ]]; then
      log "creating git backup before cleanup"
      bash "${ROOT}/scripts/git_backup_before_cleanup.sh"
    else
      log "ERROR: scripts/git_backup_before_cleanup.sh is missing"
      exit 1
    fi
  else
    log "WARNING: git backup skipped by --skip-git-backup"
  fi
  mkdir -p "${ARCHIVE_DIR}"
  log "archive dir: ${ARCHIVE_DIR}"
fi

# ---------------------------------------------------------------------------
# 1) Patch imports/routes that commonly break after manual cleanup.
# ---------------------------------------------------------------------------

patch_file "main.py" '
text = text.replace(
    "from app.modules.broker.routes import router as broker_router, legacy_router as broker_legacy_router, legacy_router as broker_legacy_router, legacy_router as broker_legacy_router",
    "from app.modules.broker.routes import router as broker_router, legacy_router as broker_legacy_router",
)
old = """        protected_paths = (
            \"/dashboard\", \"/papertrading\", \"/paper-trade-bot\", \"/paper-trade-option-bot\",
            \"/auth/zero-dte-friday\", \"/auth/schwab\", \"/auth/trade\",
            \"/auth/dashboard\", \"/auth/papertrading\", \"/auth/paper-trade-bot\", \"/auth/paper-trade-option-bot\",
            \"/auth/papertradebot\", \"/auth/trade/ui\",
            \"/auth/replay\",

        )
"""
new = """        protected_paths = (
            \"/auth/dashboard\",
            \"/auth/papertradebot\",
            \"/auth/replay\",
            \"/auth/schwab\",
            \"/auth/trade\",
            \"/broker\",
            \"/trade\",
            \"/replay-simulator\",
            \"/analysis\",
            \"/account\",
        )
"""
if old in text:
    text = text.replace(old, new)
'

patch_file "modules/users/routes.py" '
text = text.replace("from app.routes.user import router as user_router\\n", "")
text = text.replace("router.include_router(user_router)\\n\\n", "")
text = text.replace("router.include_router(user_router)\\n", "")
'

patch_file "routes/auth.py" '
for line in [
    "from passlib.hash import pbkdf2_sha256 \\n",
    "from fastapi.responses import HTMLResponse\\n",
    "from app.forms.account import UpdateAccountForm\\n",
]:
    text = text.replace(line, "")
text = text.replace("from app.config import settings\\nfrom app.services.email_service import EmailService\\nfrom werkzeug.security import check_password_hash\\nfrom app.config import settings\\n", "from app.config import settings\\nfrom app.services.email_service import EmailService\\nfrom werkzeug.security import check_password_hash\\n")

start = text.find("@router.get(\\"/notifications\\", name=\\"notifications_page\\")")
end = text.find("@router.get(\\"/verify/{token}\\", name=\\"verify_email\\")")
if start != -1 and end != -1 and end > start:
    text = text[:start] + text[end:]
'

patch_file "scripts/research/mm_features2_builder.py" '
text = text.replace(
    "from app.scripts.research.algomm_feature_sets import *  # noqa: F401,F403",
    "from app.scripts.research.Featureset_2 import *  # noqa: F401,F403",
)
'

patch_file "templates/account.html" '
text = text.replace(
    "{{ request.headers.get('\''x-forwarded-prefix'\'', '\'\'') }}/dashboard",
    "{{ request.headers.get('\''x-forwarded-prefix'\'', '\'\'') }}/auth/dashboard",
)
'

patch_file "templates/trade.html" '
text = text.replace(
    "<select id=\\"assetType\\"><option>EQUITY</option><option>OPTION</option></select>",
    "<select id=\\"assetType\\"><option>EQUITY</option></select>",
)
'

# ---------------------------------------------------------------------------
# 2) Archive unwanted live files/directories.
# ---------------------------------------------------------------------------

UNWANTED_PATHS=(
  "client_overrides"
  "refactor.py"

  "routes/account_routes.py"
  "routes/alerts.py"
  "routes/dashboard.py"
  "routes/exchange_token.py"
  "routes/internal_logger.py"
  "routes/paper_close.py"
  "routes/paper_trading.py"
  "routes/plot_routes.py"
  "routes/schwab_login.py"
  "routes/user.py"

  "tasks/history.py"

  "utils/common/db_cleanup_paper_bot.py"
  "utils/common/db_snapshot.py"
  "utils/common/plan_limiter.py"
  "utils/logging_config.py"
  "utils/stock/marketdata_api.py"

  "templates/account_mvp.html"
  "templates/agreement.html"
  "templates/ai_evaluate.html"
  "templates/candlestick_chart.html"
  "templates/candlestick_chart_daytrade.html"
  "templates/candlestick_input.html"
  "templates/check_algo.html"
  "templates/check_algo_page.html"
  "templates/daily_picks_results.html"
  "templates/dashboard.html"
  "templates/db_admin.html"
  "templates/forum.html"
  "templates/index.html"
  "templates/notifications.html"
  "templates/paper_trade_bot.html"
  "templates/paper_trading.html"
  "templates/paper_trading_bot.html"
  "templates/performance.html"
  "templates/predict_price.html"
  "templates/schwab.html"
  "templates/schwab_db.html"
  "templates/schwab_quotes.html"
  "templates/stock_news.html"
  "templates/td_dashboard.html"
  "templates/visualize_stock.html"
  "templates/auth/schwab.html"
  "templates/auth/schwab_history.html"
  "templates/auth/schwab_trade_ui.html"
  "templates/users"

  "scripts/AI_eveluate_stg_v2.py"
  "scripts/algo_debug.py"
  "scripts/analysis_engine.py"
  "scripts/chart_analysis_daytrade.py"
  "scripts/db_check_trades"
  "scripts/db_check_trades.py"
  "scripts/eod_close_open_trades.py"
  "scripts/export_history.py"
  "scripts/extract_algomm_orders.py"
  "scripts/fetch_history_probe.py"
  "scripts/one_time_schwab_refresh.py"
  "scripts/one_time_trade_token_exchange.py"
  "scripts/plot_bot_day.py"
  "scripts/schwab_test.py"
  "scripts/test_live_qcom.py"
  "scripts/test_smart_picker.py"
  "scripts/trades_working.py"
  "scripts/trigger_bot_test.py"
  "scripts/tsla_algomm_orders.csv"
  "scripts/validate_history_pipeline.py"
  "scripts/vwap-breakout.py"
  "scripts/admin_scripts"
)

for rel in "${UNWANTED_PATHS[@]}"; do
  archive_path "${rel}"
done

# ---------------------------------------------------------------------------
# 3) Optional import cleanup if autoflake is installed.
# ---------------------------------------------------------------------------

if python - <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("autoflake") else 1)
PY
then
  log "autoflake found; removing unused imports from live Python files"
  if [[ "${DRY_RUN}" != "1" ]]; then
    mapfile -t PY_FILES < <(find "${ROOT}" -type f -name '*.py' \
      ! -path "${ROOT}/_cleanup_archive_*/*" \
      ! -path '*/__pycache__/*')
    if [[ "${#PY_FILES[@]}" -gt 0 ]]; then
      python -m autoflake --in-place --remove-all-unused-imports --remove-unused-variables "${PY_FILES[@]}"
    fi
  fi
else
  log "autoflake not installed; skipped broad unused-import cleanup"
fi

# ---------------------------------------------------------------------------
# 4) Syntax check live Python files.
# ---------------------------------------------------------------------------

log "syntax check live Python files"
if [[ "${DRY_RUN}" != "1" ]]; then
  ROOT="${ROOT}" python - <<'PY'
from pathlib import Path
import os
import sys

root = Path(os.environ["ROOT"])
errors = []
count = 0

for path in root.rglob("*.py"):
    parts = set(path.parts)
    if "__pycache__" in parts or any(part.startswith("_cleanup_archive_") for part in parts):
        continue
    count += 1
    try:
        compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    except Exception as exc:
        errors.append((path.relative_to(root), exc))

if errors:
    for path, exc in errors:
        print(f"{path}: {exc}")
    sys.exit(1)

print(f"syntax ok: {count} files")
PY
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry run complete"
else
  log "cleanup complete"
  log "archived files in ${ARCHIVE_DIR}"
fi
