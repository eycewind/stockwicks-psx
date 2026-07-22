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
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/stonxs}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DB_SOURCE="${DB_SOURCE:-stockwicks_${SOURCE_CLIENT}}"
DB_NAME="stockwicks_${CLIENT_SLUG}"
DB_USER="stockwicks_${CLIENT_SLUG}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 18)}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

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
}

backup_common_file() {
  local file="$1"
  if [[ -e "${file}" ]]; then
    cp -a "${file}" "${file}.bak.${TIMESTAMP}"
    echo "Backed up ${file} -> ${file}.bak.${TIMESTAMP}"
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
    "${SOURCE_ROOT}/" "${TARGET_ROOT}/"

  mkdir -p "${TARGET_ROOT}/data/3" "${TARGET_ROOT}/logs" "${TARGET_ROOT}/models"
  chown -R www-data:www-data "${TARGET_ROOT}"
  chmod 750 "${TARGET_ROOT}" "${TARGET_ROOT}/data" "${TARGET_ROOT}/logs"
  chmod 770 "${TARGET_ROOT}/data/3" "${TARGET_ROOT}/logs"
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
      *) echo "${line}" ;;
    esac
  done < "${SOURCE_ROOT}/.env" > "${env_out}"

  chown www-data:www-data "${env_out}"
  chmod 640 "${env_out}"
}

create_database() {
  sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
SQL

  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
  sudo -u postgres pg_dump --schema-only --no-owner --no-privileges "${DB_SOURCE}" \
    | sudo -u postgres psql -v ON_ERROR_STOP=1 "${DB_NAME}"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d "${DB_NAME}" <<SQL
REASSIGN OWNED BY postgres TO ${DB_USER};
ALTER SCHEMA public OWNER TO ${DB_USER};
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
GRANT ALL ON SCHEMA public TO ${DB_USER};
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
if marker not in text:
    raise SystemExit("Could not find Production main app marker in nginx site.")
site.write_text(text.replace(marker, block + "\n\n" + marker, 1))
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
  require_root
  validate_inputs

  copy_client_tree
  write_env
  create_database
  write_systemd_services
  write_client_restart_script
  create_venv
  insert_nginx_client_block

  systemctl daemon-reload
  nginx -t
  systemctl enable --now "stockwicks-${CLIENT_SLUG}-web" "stockwicks-${CLIENT_SLUG}-celery" "stockwicks-${CLIENT_SLUG}-beat" "stockwicks-${CLIENT_SLUG}-sparkie-weekly.timer"

  echo
  echo "Created client ${CLIENT_SLUG}"
  echo "Root: ${TARGET_ROOT}"
  echo "URL: https://www.stockwicks.com/clients/${CLIENT_SLUG}"
  echo "DB: ${DB_NAME}"
  echo "DB user: ${DB_USER}"
  echo "Redis DB: ${REDIS_DB}"
  echo "Port: ${APP_PORT}"
  echo
  echo "Started services:"
  echo "  stockwicks-${CLIENT_SLUG}-web"
  echo "  stockwicks-${CLIENT_SLUG}-celery"
  echo "  stockwicks-${CLIENT_SLUG}-beat"
  print_oauth_routing_note
}

main "$@"
