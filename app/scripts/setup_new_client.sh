#!/usr/bin/env bash
set -euo pipefail

# Create a new StockWicks commercial client by mimicking the existing ashakil
# deployment. Run on the Ubuntu server as a sudo-capable user.
#
# Example:
#   sudo bash scripts/setup_new_client.sh ahaitham 8102 2
#
# Arguments:
#   1. client slug, lowercase letters/numbers/underscore/hyphen
#   2. app port, unique localhost port for uvicorn
#   3. redis db number, unique Redis DB for this client's celery queues

SOURCE_CLIENT="${SOURCE_CLIENT:-ashakil}"
CLIENT_SLUG="${1:-}"
APP_PORT="${2:-}"
REDIS_DB="${3:-}"

CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
SOURCE_ROOT="${CLIENTS_ROOT}/${SOURCE_CLIENT}"
TARGET_ROOT="${CLIENTS_ROOT}/${CLIENT_SLUG}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-enabled/stonxs}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DB_SOURCE="${DB_SOURCE:-stockwicks_${SOURCE_CLIENT}}"
DB_NAME="stockwicks_${CLIENT_SLUG}"
DB_USER="stockwicks_${CLIENT_SLUG}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 18)}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SETUP_LOG_DIR="${SETUP_LOG_DIR:-/var/stockwicks/client_setup_logs}"
SETUP_LOG="${SETUP_LOG_DIR}/setup_${CLIENT_SLUG:-unknown}_${TIMESTAMP}.log"
NGINX_BACKUP_DIR="${NGINX_BACKUP_DIR:-/var/stockwicks/nginx_backups}"

start_logging() {
  mkdir -p "${SETUP_LOG_DIR}"
  exec > >(tee -a "${SETUP_LOG}") 2>&1
  echo "StockWicks client setup log"
  echo "Started: $(date -Is)"
  echo "Client: ${CLIENT_SLUG:-}"
  echo "Source client: ${SOURCE_CLIENT}"
  echo "Target root: ${TARGET_ROOT}"
  echo "App port: ${APP_PORT:-}"
  echo "Redis DB: ${REDIS_DB:-}"
  echo "Log file: ${SETUP_LOG}"
  echo
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "Run this script with sudo/root."
  fi
}

validate_inputs() {
  [[ -n "${CLIENT_SLUG}" ]] || die "Missing client slug."
  [[ -n "${APP_PORT}" ]] || die "Missing app port."
  [[ -n "${REDIS_DB}" ]] || die "Missing Redis DB number."
  [[ "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${CLIENT_SLUG}"
  [[ "${APP_PORT}" =~ ^[0-9]+$ ]] || die "APP_PORT must be numeric."
  [[ "${REDIS_DB}" =~ ^[0-9]+$ ]] || die "REDIS_DB must be numeric."
  [[ -d "${SOURCE_ROOT}" ]] || die "Missing source client root: ${SOURCE_ROOT}"
  [[ -f "${SOURCE_ROOT}/.env" ]] || die "Missing source env: ${SOURCE_ROOT}/.env"
  [[ ! -e "${TARGET_ROOT}" ]] || die "Target already exists: ${TARGET_ROOT}"
  command -v rsync >/dev/null || die "rsync is required."
  command -v psql >/dev/null || die "psql is required."
  command -v pg_dump >/dev/null || die "pg_dump is required."
  command -v ss >/dev/null || die "ss is required."
  if ss -ltn "( sport = :${APP_PORT} )" | grep -q ":${APP_PORT}"; then
    die "Port ${APP_PORT} is already in use."
  fi
  if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    die "Database already exists: ${DB_NAME}"
  fi
  if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
    die "Database role already exists: ${DB_USER}"
  fi
}

backup_common_file() {
  local file="$1"
  if [[ -e "${file}" ]]; then
    mkdir -p "${NGINX_BACKUP_DIR}"
    local backup="${NGINX_BACKUP_DIR}/$(basename "${file}").bak.${TIMESTAMP}"
    cp -a "${file}" "${backup}"
    echo "Backed up ${file} -> ${backup}"
  fi
}

copy_client_tree() {
  mkdir -p "${TARGET_ROOT}"

  rsync -a \
    --exclude '.env' \
    --exclude 'venv' \
    --exclude 'data/*' \
    --exclude 'logs/*' \
    --exclude 'env_backup' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.bak' \
    --exclude '*.bak.*' \
    --exclude '_cleanup_archive_*' \
    --exclude 'app.broken_*' \
    --exclude '.git' \
    "${SOURCE_ROOT}/" "${TARGET_ROOT}/"

  mkdir -p "${TARGET_ROOT}/data" "${TARGET_ROOT}/logs" "${TARGET_ROOT}/models"
  chown -R www-data:www-data "${TARGET_ROOT}"
  chmod 750 "${TARGET_ROOT}" "${TARGET_ROOT}/data" "${TARGET_ROOT}/logs"
  chmod 770 "${TARGET_ROOT}/data" "${TARGET_ROOT}/logs"
}

patch_target_client_code() {
  TARGET_ROOT="${TARGET_ROOT}" python3 <<'PY'
from pathlib import Path
import os

root = Path(os.environ["TARGET_ROOT"])
app = root / "app"

def replace(path: Path, old: str, new: str) -> None:
    if not path.exists():
        print(f"Patch skip, missing file: {path}")
        return
    text = path.read_text()
    if old not in text:
        return
    path.write_text(text.replace(old, new))
    print(f"Patched {path}")

main_py = app / "main.py"
if main_py.exists():
    text = main_py.read_text()
    needle = 'app = FastAPI(title="StockWicks API")'
    insert = needle + "\napp.state.client_slug = settings.client_slug"
    if needle in text and "app.state.client_slug" not in text:
        text = text.replace(needle, insert, 1)
    # Keep the source client's router layout. The working ashakil deployment
    # still includes schwab_auth.router, and shared OAuth callbacks depend on
    # matching that behavior.
    text = text.replace(
        "# Disabled for commercial clients: app.include_router(schwab_auth.router)",
        "app.include_router(schwab_auth.router)",
    )
    main_py.write_text(text)
    print(f"Patched {main_py}")

base_html = app / "templates" / "base.html"
if base_html.exists():
    text = base_html.read_text()
    if "request.app.state.client_slug" not in text:
        text = text.replace(
            '{% set url_prefix = request.headers.get("x-forwarded-prefix", "") if request is defined else "" %}',
            '{% set url_prefix = request.headers.get("x-forwarded-prefix", "") if request is defined else "" %}\n'
            '{% set client_slug = request.app.state.client_slug|default("client") if request is defined else "client" %}',
            1,
        )
    text = text.replace('<span class="client-badge">ashakil</span>', '<span class="client-badge">{{ client_slug }}</span>')
    base_html.write_text(text)
    print(f"Patched {base_html}")

dashboard_html = app / "templates" / "dashboard" / "index.html"
if dashboard_html.exists():
    text = dashboard_html.read_text()
    if "request.app.state.client_slug" not in text:
        text = text.replace(
            '{% set url_prefix = request.headers.get("x-forwarded-prefix", "") if request is defined else "" %}',
            '{% set url_prefix = request.headers.get("x-forwarded-prefix", "") if request is defined else "" %}\n'
            '{% set client_slug = request.app.state.client_slug|default("client") if request is defined else "client" %}',
            1,
        )
    text = text.replace("Client: ashakil", "Client: {{ client_slug }}")
    dashboard_html.write_text(text)
    print(f"Patched {dashboard_html}")

trade_bot_html = app / "templates" / "td_paper_trade_bot.html"
if trade_bot_html.exists():
    text = trade_bot_html.read_text()
    if '{% set url_prefix = request.headers.get("x-forwarded-prefix", "") if request is defined else "" %}' not in text:
        text = text.replace(
            "{% block content %}",
            '{% block content %}\n{% set url_prefix = request.headers.get("x-forwarded-prefix", "") if request is defined else "" %}',
            1,
        )
    text = text.replace('window.STOCKWICKS_CLIENT_PREFIX = "/clients/ashakil";', 'window.STOCKWICKS_CLIENT_PREFIX = "{{ url_prefix }}";')
    text = text.replace('/clients/ashakil/auth/papertradebot/start', '{{ url_prefix }}/auth/papertradebot/start')
    text = text.replace('/clients/ashakil/auth/papertradebot/save-size-restart/', '{{ url_prefix }}/auth/papertradebot/save-size-restart/')
    text = text.replace('/clients/ashakil/auth/papertradebot/stop/', '{{ url_prefix }}/auth/papertradebot/stop/')
    text = text.replace('/clients/ashakil/auth/papertradebot/cancel/', '{{ url_prefix }}/auth/papertradebot/cancel/')
    trade_bot_html.write_text(text)
    print(f"Patched {trade_bot_html}")

log_js = app / "static" / "js" / "log_analysis.js"
replace(
    log_js,
    '(window.location.pathname.startsWith("/clients/ashakil") ? "/clients/ashakil" : "");',
    '((window.location.pathname.match(/^\\/clients\\/[^/]+/) || [""])[0]);',
)
PY
}

update_oauth_dispatcher() {
  [[ -f "${NGINX_SITE}" ]] || die "Missing nginx site file: ${NGINX_SITE}"
  backup_common_file "${NGINX_SITE}"

  python3 - "$NGINX_SITE" "$CLIENT_SLUG" "$APP_PORT" <<'PY'
from pathlib import Path
import re
import sys

site = Path(sys.argv[1])
client = sys.argv[2]
port = sys.argv[3]
text = site.read_text()

upstream_line = f"    ~^{client}(:|%3A) 127.0.0.1:{port};"
prefix_line = f"    ~^{client}(:|%3A) /clients/{client};"

maps = f"""
map $arg_state $stockwicks_oauth_upstream {{
    default 127.0.0.1:8101;
    ~^ashakil(:|%3A) 127.0.0.1:8101;
    {upstream_line.strip()}
}}

map $arg_state $stockwicks_oauth_prefix {{
    default "";
    ~^ashakil(:|%3A) /clients/ashakil;
    {prefix_line.strip()}
}}

"""

if "map $arg_state $stockwicks_oauth_upstream" not in text:
    first_server = text.find("server {")
    if first_server == -1:
        raise SystemExit("Could not find first nginx server block for OAuth maps.")
    text = text[:first_server] + maps + text[first_server:]
else:
    # Remove stale/duplicate entries for this client from both map blocks.
    text = re.sub(
        rf"^\s*~\^{re.escape(client)}(?:\(:\|%3A\)|:)\s+127\.0\.0\.1:\d+;\n",
        "",
        text,
        flags=re.M,
    )
    text = re.sub(
        rf"^\s*~\^{re.escape(client)}(?:\(:\|%3A\)|:)\s+/clients/{re.escape(client)};\n",
        "",
        text,
        flags=re.M,
    )
    text = re.sub(
        r"(map \$arg_state \$stockwicks_oauth_upstream \{\n)",
        rf"\1{upstream_line}\n",
        text,
        count=1,
    )
    text = re.sub(
        r"(map \$arg_state \$stockwicks_oauth_prefix \{\n)",
        rf"\1{prefix_line}\n",
        text,
        count=1,
    )

callback_block = """location = /auth/callback {
        proxy_pass http://$stockwicks_oauth_upstream/auth/callback$is_args$args;
        proxy_http_version 1.1;

        proxy_set_header Host www.stockwicks.com;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Prefix $stockwicks_oauth_prefix;

        proxy_redirect / $stockwicks_oauth_prefix/;
    }"""

trade_block = """location = /auth/schwab/db/callback {
        proxy_pass http://$stockwicks_oauth_upstream/auth/schwab/db/callback$is_args$args;
        proxy_http_version 1.1;

        proxy_set_header Host www.stockwicks.com;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Prefix $stockwicks_oauth_prefix;

        proxy_redirect / $stockwicks_oauth_prefix/;
    }"""

def replace_or_insert_location(src: str, location_name: str, block: str) -> str:
    pattern = re.compile(rf"location = {re.escape(location_name)} \{{.*?\n\s*\}}", re.DOTALL)
    src, count = pattern.subn(block, src)
    if count:
        return src

    first_server = src.find("server {")
    if first_server == -1:
        raise SystemExit(f"Could not insert {location_name}; no server block found.")
    brace = src.find("{", first_server)
    return src[: brace + 1] + "\n    " + block.replace("\n", "\n    ") + "\n" + src[brace + 1 :]

text = replace_or_insert_location(text, "/auth/callback", callback_block)
text = replace_or_insert_location(text, "/auth/schwab/db/callback", trade_block)

site.write_text(text)
print(f"OAuth dispatcher updated for {client} -> 127.0.0.1:{port}")
PY
}

write_env() {
  local env_out="${TARGET_ROOT}/.env"

  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      CLIENT_SLUG=*) echo "CLIENT_SLUG=${CLIENT_SLUG}" ;;
      APP_PORT=*) echo "APP_PORT=${APP_PORT}" ;;
      PUBLIC_BASE_URL=*) echo "PUBLIC_BASE_URL=https://www.stockwicks.com/clients/${CLIENT_SLUG}" ;;
      CLIENT_PUBLIC_BASE_URL=*) echo "CLIENT_PUBLIC_BASE_URL=https://www.stockwicks.com/clients/${CLIENT_SLUG}" ;;
      CLIENT_PUBLIC_PREFIX=*) echo "CLIENT_PUBLIC_PREFIX=/clients/${CLIENT_SLUG}" ;;
      CLIENT_ROOT=*) echo "CLIENT_ROOT=${TARGET_ROOT}" ;;
      DATA_DIR=*) echo "DATA_DIR=${TARGET_ROOT}/data" ;;
      LOG_DIR=*) echo "LOG_DIR=${TARGET_ROOT}/logs" ;;
      MODEL_DIR=*) echo "MODEL_DIR=${TARGET_ROOT}/models" ;;
      DATABASE_URL=*) echo "DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}" ;;
      REDIS_URL=*) echo "REDIS_URL=redis://127.0.0.1:6379/${REDIS_DB}" ;;
      CELERY_BROKER_URL=*) echo "CELERY_BROKER_URL=redis://127.0.0.1:6379/${REDIS_DB}" ;;
      CELERY_RESULT_BACKEND=*) echo "CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/${REDIS_DB}" ;;
      SCHWAB_REFRESH_USER_ID=*) echo "SCHWAB_REFRESH_USER_ID=1" ;;
      *) echo "${line}" ;;
    esac
  done < "${SOURCE_ROOT}/.env" > "${env_out}"

  chown www-data:www-data "${env_out}"
  chmod 640 "${env_out}"
}

create_database() {
  local quoted_db_name="\"${DB_NAME}\""
  local quoted_db_user="\"${DB_USER}\""

  sudo -u postgres psql -v ON_ERROR_STOP=1 -v db_user="${DB_USER}" -v db_password="${DB_PASSWORD}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'db_user', :'db_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'db_user') \gexec
SQL

  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"

  {
    echo "SET ROLE ${quoted_db_user};"
    sudo -u postgres pg_dump --schema-only --no-owner --no-privileges "${DB_SOURCE}"
    echo "RESET ROLE;"
  } | sudo -u postgres psql -v ON_ERROR_STOP=1 "${DB_NAME}"

  sudo -u postgres psql -v ON_ERROR_STOP=1 -d "${DB_NAME}" <<SQL
ALTER DATABASE ${quoted_db_name} OWNER TO ${quoted_db_user};
ALTER SCHEMA public OWNER TO ${quoted_db_user};
GRANT ALL PRIVILEGES ON DATABASE ${quoted_db_name} TO ${quoted_db_user};
GRANT ALL ON SCHEMA public TO ${quoted_db_user};
SQL
}

write_systemd_services() {
  cat > "${SYSTEMD_DIR}/stockwicks-${CLIENT_SLUG}-web.service" <<EOF
[Unit]
Description=StockWicks ${CLIENT_SLUG} FastAPI Web
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=${TARGET_ROOT}
EnvironmentFile=${TARGET_ROOT}/.env
ExecStart=${TARGET_ROOT}/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  cat > "${SYSTEMD_DIR}/stockwicks-${CLIENT_SLUG}-celery.service" <<EOF
[Unit]
Description=StockWicks ${CLIENT_SLUG} Celery Worker
After=network.target redis-server.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=${TARGET_ROOT}
EnvironmentFile=${TARGET_ROOT}/.env
ExecStart=${TARGET_ROOT}/venv/bin/celery -A app.celery_worker.celery worker --loglevel=INFO --concurrency=2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  cat > "${SYSTEMD_DIR}/stockwicks-${CLIENT_SLUG}-beat.service" <<EOF
[Unit]
Description=StockWicks ${CLIENT_SLUG} Celery Beat
After=network.target redis-server.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=${TARGET_ROOT}
EnvironmentFile=${TARGET_ROOT}/.env
ExecStart=${TARGET_ROOT}/venv/bin/celery -A app.celery_worker.celery beat --loglevel=INFO --schedule=${TARGET_ROOT}/logs/celerybeat-schedule.db
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  cat > "${SYSTEMD_DIR}/stockwicks-${CLIENT_SLUG}-sparkie-weekly.service" <<EOF
[Unit]
Description=StockWicks ${CLIENT_SLUG} Weekly Sparkie Research
After=network.target postgresql.service

[Service]
Type=oneshot
User=www-data
Group=www-data
WorkingDirectory=${TARGET_ROOT}
EnvironmentFile=${TARGET_ROOT}/.env
ExecStart=${TARGET_ROOT}/venv/bin/python -m app.scripts.sparkie_weekly_scheduler
TimeoutStartSec=infinity
EOF

  cat > "${SYSTEMD_DIR}/stockwicks-${CLIENT_SLUG}-sparkie-weekly.timer" <<EOF
[Unit]
Description=Run StockWicks ${CLIENT_SLUG} Weekly Sparkie every Sunday

[Timer]
OnCalendar=Sun *-*-* 02:00:00 America/New_York
Persistent=true
Unit=stockwicks-${CLIENT_SLUG}-sparkie-weekly.service

[Install]
WantedBy=timers.target
EOF
}

write_client_restart_script() {
  local restart_script="${TARGET_ROOT}/restart_services.sh"

  cat > "${restart_script}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

# Restart all services for this StockWicks client.
# Usage:
#   sudo bash ${TARGET_ROOT}/restart_services.sh

CLIENT_SLUG="${CLIENT_SLUG}"

if [[ "\${EUID}" -ne 0 ]]; then
  echo "ERROR: Run this script with sudo/root." >&2
  exit 1
fi

SERVICES=(
  "stockwicks-\${CLIENT_SLUG}-web"
  "stockwicks-\${CLIENT_SLUG}-celery"
  "stockwicks-\${CLIENT_SLUG}-beat"
)

systemctl daemon-reload
systemctl restart "\${SERVICES[@]}"
systemctl status --no-pager "\${SERVICES[@]}"
EOF

  chown www-data:www-data "${restart_script}"
  chmod 750 "${restart_script}"
}

create_venv() {
  local python_bin="${PYTHON_BIN:-python3}"
  local pip_bin="${TARGET_ROOT}/venv/bin/pip"

  sudo -u www-data "${python_bin}" -m venv "${TARGET_ROOT}/venv"
  sudo -u www-data "${TARGET_ROOT}/venv/bin/pip" install --upgrade pip

  if [[ -f "${TARGET_ROOT}/requirements.txt" ]]; then
    sudo -u www-data "${pip_bin}" install -r "${TARGET_ROOT}/requirements.txt"
  elif compgen -G "${TARGET_ROOT}/requirements*.txt" > /dev/null; then
    for req_file in "${TARGET_ROOT}"/requirements*.txt; do
      sudo -u www-data "${pip_bin}" install -r "${req_file}"
    done
  else
    die "No requirements files found in ${TARGET_ROOT}; install dependencies before starting services."
  fi
}

insert_nginx_client_block() {
  [[ -f "${NGINX_SITE}" ]] || die "Missing nginx site file: ${NGINX_SITE}"
  backup_common_file "${NGINX_SITE}"

  if grep -q "location /clients/${CLIENT_SLUG}/" "${NGINX_SITE}"; then
    echo "nginx client block already exists for ${CLIENT_SLUG}"
    return
  fi

  local block="/tmp/stockwicks_${CLIENT_SLUG}_nginx_block.${TIMESTAMP}"
  cat > "${block}" <<EOF

    # ============================
    # Commercial client: ${CLIENT_SLUG}
    # ============================
    location /clients/${CLIENT_SLUG}/ {
        add_header X-StockWicks-Client ${CLIENT_SLUG} always;

        proxy_pass http://127.0.0.1:${APP_PORT}/;
        proxy_http_version 1.1;

        proxy_set_header Host www.stockwicks.com;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Prefix /clients/${CLIENT_SLUG};

        proxy_redirect / /clients/${CLIENT_SLUG}/;
    }
EOF

  python3 - "$NGINX_SITE" "$block" <<'PY'
from pathlib import Path
import sys

site = Path(sys.argv[1])
block = Path(sys.argv[2]).read_text()
text = site.read_text()

marker = "    # ============================\n    # Production main app"
if marker in text:
    site.write_text(text.replace(marker, block + "\n\n" + marker, 1))
    raise SystemExit(0)

# Fallback for configs without comments: insert after the existing ashakil
# client location block.
ashakil = "location /clients/ashakil/"
start = text.find(ashakil)
if start != -1:
    brace_start = text.find("{", start)
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                insert_at = i + 1
                site.write_text(text[:insert_at] + "\n" + block + text[insert_at:])
                raise SystemExit(0)

raise SystemExit(
    "Could not find where to insert client block. "
    "Use NGINX_SITE=/etc/nginx/sites-enabled/stonxs if your active config is there, "
    "or add the /clients/<client>/ block manually."
)
PY
}

print_oauth_routing_note() {
  cat <<EOF

IMPORTANT OAUTH ROUTING NOTE
----------------------------
The Schwab callback URLs can stay shared:
  https://www.stockwicks.com/auth/callback
  https://www.stockwicks.com/auth/schwab/db/callback

For multiple clients, those root callback locations must route by the OAuth
state prefix (${CLIENT_SLUG}:...). If they stay hardwired to ashakil/8101,
new clients will not save tokens in their own data directory.

After setup, confirm nginx root callback routing before client OAuth.
EOF
}

main() {
  start_logging
  require_root
  validate_inputs

  copy_client_tree
  patch_target_client_code
  chown -R www-data:www-data "${TARGET_ROOT}"
  write_env
  create_database
  write_systemd_services
  write_client_restart_script
  create_venv
  insert_nginx_client_block
  update_oauth_dispatcher

  systemctl daemon-reload
  nginx -t
  systemctl reload nginx
  systemctl enable --now "stockwicks-${CLIENT_SLUG}-web" "stockwicks-${CLIENT_SLUG}-celery" "stockwicks-${CLIENT_SLUG}-beat" "stockwicks-${CLIENT_SLUG}-sparkie-weekly.timer"
  systemctl status --no-pager "stockwicks-${CLIENT_SLUG}-web" "stockwicks-${CLIENT_SLUG}-celery" "stockwicks-${CLIENT_SLUG}-beat"

  echo
  echo "Created client ${CLIENT_SLUG}"
  echo "Root: ${TARGET_ROOT}"
  echo "URL: https://www.stockwicks.com/clients/${CLIENT_SLUG}"
  echo "DB: ${DB_NAME}"
  echo "DB user: ${DB_USER}"
  echo "Redis DB: ${REDIS_DB}"
  echo "Port: ${APP_PORT}"
  echo "Setup log: ${SETUP_LOG}"
  echo
  echo "Started services:"
  echo "  stockwicks-${CLIENT_SLUG}-web"
  echo "  stockwicks-${CLIENT_SLUG}-celery"
  echo "  stockwicks-${CLIENT_SLUG}-beat"
  print_oauth_routing_note
}

main "$@"
