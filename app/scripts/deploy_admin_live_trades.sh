#!/usr/bin/env bash
set -euo pipefail

# Configure and deploy the admin live-trades web panel for one StockWicks client.
#
# Run from a deployed client root, usually ashakil:
#   cd /var/stockwicks/clients/ashakil
#   sudo bash app/scripts/deploy_admin_live_trades.sh --apply
#   sudo bash app/scripts/deploy_admin_live_trades.sh --apply --clients auto
#
# Default is a dry run. Add --apply to change Postgres grants, .env, admin user,
# and restart the web service. On apply, this script saves .env to
# .admin_live_trades_backups/ before editing it. Roll back with:
#   sudo bash app/scripts/rollback_admin_live_trades.sh --apply

CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
ADMIN_CLIENT="${ADMIN_CLIENT:-}"
ADMIN_USER="${ADMIN_USER:-ratadmin}"
REPORT_USER="${REPORT_USER:-stockwicks_reporter}"
REPORT_PASSWORD="${REPORT_PASSWORD:-}"
CLIENT_SLUGS="${ADMIN_CLIENT_SLUGS:-auto}"
APPLY=0
RESTART=1

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  sudo bash app/scripts/deploy_admin_live_trades.sh [--apply] [options]

Options:
  --apply                 Make changes. Without this, print the plan only.
  --admin-client SLUG     Client hosting the admin UI. Default: current folder name.
  --clients auto|a,b,c    Client DB slugs to report on. Default: auto.
  --admin-user USERNAME   User to mark is_admin=true. Default: ratadmin.
  --report-user USER      Read-only Postgres role. Default: stockwicks_reporter.
  --report-password PASS  Password for report role. Default: reuse .env value or generate.
  --no-restart            Do not restart stockwicks-<admin-client>-web.
  -h, --help              Show help.

Environment overrides use the same names as options:
  ADMIN_CLIENT, ADMIN_CLIENT_SLUGS, ADMIN_USER, REPORT_USER, REPORT_PASSWORD
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --admin-client)
      ADMIN_CLIENT="${2:-}"
      [[ -n "${ADMIN_CLIENT}" ]] || die "--admin-client requires a value"
      shift 2
      ;;
    --clients)
      CLIENT_SLUGS="${2:-}"
      [[ -n "${CLIENT_SLUGS}" ]] || die "--clients requires a comma-separated value"
      shift 2
      ;;
    --admin-user)
      ADMIN_USER="${2:-}"
      [[ -n "${ADMIN_USER}" ]] || die "--admin-user requires a value"
      shift 2
      ;;
    --report-user)
      REPORT_USER="${2:-}"
      [[ -n "${REPORT_USER}" ]] || die "--report-user requires a value"
      shift 2
      ;;
    --report-password)
      REPORT_PASSWORD="${2:-}"
      [[ -n "${REPORT_PASSWORD}" ]] || die "--report-password requires a value"
      shift 2
      ;;
    --no-restart)
      RESTART=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

ROOT="$(pwd)"
[[ -f "${ROOT}/app/main.py" ]] || die "Run from a StockWicks client root containing app/main.py"
[[ -f "${ROOT}/.env" ]] || die "Missing .env in ${ROOT}"
BACKUP_DIR="${ROOT}/.admin_live_trades_backups"

if [[ -z "${ADMIN_CLIENT}" ]]; then
  ADMIN_CLIENT="$(basename "${ROOT}")"
fi

[[ "${ADMIN_CLIENT}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid admin client slug: ${ADMIN_CLIENT}"
[[ "${REPORT_USER}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die "Invalid Postgres role name: ${REPORT_USER}"

VALID_CLIENTS=()
EXISTING_CLIENTS=()

extract_env_value() {
  local key="$1"
  local file="$2"
  grep -E "^${key}=" "${file}" | tail -n 1 | cut -d= -f2- || true
}

existing_template="$(extract_env_value "ADMIN_CLIENT_DATABASE_URL_TEMPLATE" "${ROOT}/.env")"
if [[ -z "${REPORT_PASSWORD}" && "${existing_template}" =~ postgresql://([^:]+):([^@]+)@ ]]; then
  REPORT_PASSWORD="${BASH_REMATCH[2]}"
fi
if [[ -z "${REPORT_PASSWORD}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    REPORT_PASSWORD="$(openssl rand -hex 24)"
  else
    REPORT_PASSWORD="$(date +%s%N | sha256sum | awk '{print substr($1,1,48)}')"
  fi
fi

REPORT_DSN_TEMPLATE="postgresql://${REPORT_USER}:${REPORT_PASSWORD}@localhost:5432/stockwicks_{slug}"

echo "Admin live-trades deploy plan"
echo "Root: ${ROOT}"
echo "Admin client: ${ADMIN_CLIENT}"
echo "Admin user: ${ADMIN_USER}"
echo "Report clients: ${CLIENT_SLUGS}"
echo "Report role: ${REPORT_USER}"
if [[ "${APPLY}" -eq 0 ]]; then
  echo "Mode: DRY RUN. Re-run with --apply to make changes."
else
  echo "Mode: APPLY"
fi

run_or_print() {
  if [[ "${APPLY}" -eq 1 ]]; then
    "$@"
  else
    printf 'DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  fi
}

backup_env() {
  local stamp
  stamp="$(date +%Y%m%d_%H%M%S)"
  if [[ "${APPLY}" -eq 0 ]]; then
    echo "DRY RUN: backup ${ROOT}/.env to ${BACKUP_DIR}/.env.${stamp}.bak"
    return
  fi
  mkdir -p "${BACKUP_DIR}"
  cp -a "${ROOT}/.env" "${BACKUP_DIR}/.env.${stamp}.bak"
  echo "Backed up .env to ${BACKUP_DIR}/.env.${stamp}.bak"
}

psql_postgres() {
  if [[ "${APPLY}" -eq 1 ]]; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 "$@"
  else
    printf 'DRY RUN: sudo -u postgres psql'
    printf ' %q' "$@"
    printf '\n'
  fi
}

database_exists() {
  local db_name="$1"
  if [[ "${APPLY}" -eq 0 ]]; then
    return 0
  fi
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db_name}'" | grep -q 1
}

discover_client_slugs() {
  if [[ "${APPLY}" -eq 0 ]]; then
    echo "${ADMIN_CLIENT}"
    return
  fi
  sudo -u postgres psql -tAc "
    SELECT regexp_replace(datname, '^stockwicks_', '')
    FROM pg_database
    WHERE datname LIKE 'stockwicks\_%' ESCAPE '\'
    ORDER BY datname;
  " | awk 'NF { gsub(/^[ \t]+|[ \t]+$/, ""); print }'
}

resolve_requested_clients() {
  VALID_CLIENTS=()
  if [[ "${CLIENT_SLUGS}" == "auto" || "${CLIENT_SLUGS}" == "ALL" || "${CLIENT_SLUGS}" == "all" ]]; then
    mapfile -t VALID_CLIENTS < <(discover_client_slugs)
  else
    local client
    IFS=',' read -r -a CLIENTS <<< "${CLIENT_SLUGS}"
    for client in "${CLIENTS[@]}"; do
      client="$(echo "${client}" | xargs)"
      [[ -n "${client}" ]] || continue
      [[ "${client}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${client}"
      VALID_CLIENTS+=("${client}")
    done
  fi
  [[ ${#VALID_CLIENTS[@]} -gt 0 ]] || die "No valid clients configured"
  CLIENT_SLUGS="$(IFS=','; echo "${VALID_CLIENTS[*]}")"
}

ensure_report_role() {
  local sql
  sql=$(cat <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${REPORT_USER}') THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', '${REPORT_USER}', '${REPORT_PASSWORD}');
  ELSE
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', '${REPORT_USER}', '${REPORT_PASSWORD}');
  END IF;
END
\$\$;
SQL
)
  psql_postgres -d postgres -c "${sql}"
}

grant_client_db() {
  local client="$1"
  local db_name="stockwicks_${client}"
  local sql
  sql=$(cat <<SQL
GRANT CONNECT ON DATABASE "${db_name}" TO "${REPORT_USER}";
GRANT USAGE ON SCHEMA public TO "${REPORT_USER}";
GRANT SELECT ON TABLE users TO "${REPORT_USER}";
GRANT SELECT ON TABLE paper_stock_trade_bots TO "${REPORT_USER}";
GRANT SELECT ON TABLE paper_stock_bot_live_mirror_history TO "${REPORT_USER}";
SQL
)
  psql_postgres -d "${db_name}" -c "${sql}"
}

filter_existing_clients() {
  local client
  local db_name
  EXISTING_CLIENTS=()
  for client in "${VALID_CLIENTS[@]}"; do
    db_name="stockwicks_${client}"
    if database_exists "${db_name}"; then
      EXISTING_CLIENTS+=("${client}")
    else
      echo "WARN: database ${db_name} does not exist; skipping client ${client}."
    fi
  done
  if [[ ${#EXISTING_CLIENTS[@]} -eq 0 ]]; then
    die "No configured client databases exist. Checked: ${CLIENT_SLUGS}"
  fi
  CLIENT_SLUGS="$(IFS=','; echo "${EXISTING_CLIENTS[*]}")"
}

mark_admin_user() {
  local db_name="stockwicks_${ADMIN_CLIENT}"
  local sql="UPDATE users SET is_admin = true WHERE username = '${ADMIN_USER}';"
  psql_postgres -d "${db_name}" -c "${sql}"
}

upsert_env_line() {
  local key="$1"
  local value="$2"
  local file="${ROOT}/.env"

  if [[ "${APPLY}" -eq 0 ]]; then
    echo "DRY RUN: set ${key}=<configured> in ${file}"
    return
  fi

  local tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" "${file}"; then
    awk -v key="${key}" -v value="${value}" '
      BEGIN { done = 0 }
      $0 ~ "^" key "=" {
        if (!done) {
          print key "=" value
          done = 1
        }
        next
      }
      { print }
      END {
        if (!done) print key "=" value
      }
    ' "${file}" > "${tmp}"
  else
    cat "${file}" > "${tmp}"
    printf '\n%s=%s\n' "${key}" "${value}" >> "${tmp}"
  fi
  cat "${tmp}" > "${file}"
  rm -f "${tmp}"
}

validate_app_import() {
  if [[ -x "${ROOT}/venv/bin/python" ]]; then
    run_or_print "${ROOT}/venv/bin/python" -c "import app.main; print('admin live trades import ok')"
  else
    echo "WARN: ${ROOT}/venv/bin/python not found; skipping import validation."
  fi
}

restart_web() {
  [[ "${RESTART}" -eq 1 ]] || return 0
  run_or_print systemctl restart "stockwicks-${ADMIN_CLIENT}-web"
  run_or_print systemctl status --no-pager "stockwicks-${ADMIN_CLIENT}-web"
}

ensure_report_role
resolve_requested_clients
filter_existing_clients
for client in "${EXISTING_CLIENTS[@]}"; do
  grant_client_db "${client}"
done
mark_admin_user
backup_env
upsert_env_line "ADMIN_CLIENT_SLUGS" "${CLIENT_SLUGS}"
upsert_env_line "ADMIN_CLIENT_DATABASE_URL_TEMPLATE" "${REPORT_DSN_TEMPLATE}"
validate_app_import
restart_web

echo
if [[ "${APPLY}" -eq 1 ]]; then
  echo "Admin live-trades setup complete."
  echo "Open: https://www.stockwicks.com/clients/${ADMIN_CLIENT}/admin/live-trades"
else
  echo "Dry run complete."
fi
