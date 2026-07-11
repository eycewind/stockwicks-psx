#!/usr/bin/env bash
set -euo pipefail

# Roll back deploy_admin_live_trades.sh server-side setup.
#
# Default is a dry run. Add --apply to restore the latest .env backup and
# restart the admin client's web service. Optional flags can revoke reporting
# grants and unset the admin user.

ADMIN_CLIENT="${ADMIN_CLIENT:-}"
ADMIN_USER="${ADMIN_USER:-ratadmin}"
REPORT_USER="${REPORT_USER:-stockwicks_reporter}"
CLIENT_SLUGS="${ADMIN_CLIENT_SLUGS:-ashakil,haithaima,yzia}"
APPLY=0
RESTART=1
BACKUP_FILE=""
REVOKE_GRANTS=0
UNSET_ADMIN=0

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  sudo bash app/scripts/rollback_admin_live_trades.sh [--apply] [options]

Options:
  --apply                 Make changes. Without this, print the plan only.
  --admin-client SLUG     Client hosting the admin UI. Default: current folder name.
  --clients a,b,c         Client DB slugs to revoke from if --revoke-grants is used.
  --admin-user USERNAME   User to unset if --unset-admin is used. Default: ratadmin.
  --report-user USER      Read-only Postgres role. Default: stockwicks_reporter.
  --backup PATH           Restore this .env backup instead of latest.
  --revoke-grants         Revoke report role SELECT/USAGE/CONNECT grants from client DBs.
  --unset-admin           Set admin user is_admin=false in admin client DB.
  --no-restart            Do not restart stockwicks-<admin-client>-web.
  -h, --help              Show help.

Typical rollback:
  sudo bash app/scripts/rollback_admin_live_trades.sh
  sudo bash app/scripts/rollback_admin_live_trades.sh --apply

Full access cleanup:
  sudo bash app/scripts/rollback_admin_live_trades.sh --apply --revoke-grants --unset-admin
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
    --backup)
      BACKUP_FILE="${2:-}"
      [[ -n "${BACKUP_FILE}" ]] || die "--backup requires a path"
      shift 2
      ;;
    --revoke-grants)
      REVOKE_GRANTS=1
      shift
      ;;
    --unset-admin)
      UNSET_ADMIN=1
      shift
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

IFS=',' read -r -a CLIENTS <<< "${CLIENT_SLUGS}"
VALID_CLIENTS=()
for client in "${CLIENTS[@]}"; do
  client="$(echo "${client}" | xargs)"
  [[ -n "${client}" ]] || continue
  [[ "${client}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${client}"
  VALID_CLIENTS+=("${client}")
done

if [[ -z "${BACKUP_FILE}" ]]; then
  if compgen -G "${BACKUP_DIR}/.env.*.bak" >/dev/null; then
    BACKUP_FILE="$(ls -1t "${BACKUP_DIR}"/.env.*.bak | head -n 1)"
  else
    BACKUP_FILE=""
  fi
fi

echo "Admin live-trades rollback plan"
echo "Root: ${ROOT}"
echo "Admin client: ${ADMIN_CLIENT}"
echo "Env backup: ${BACKUP_FILE:-none found}"
echo "Revoke grants: ${REVOKE_GRANTS}"
echo "Unset admin: ${UNSET_ADMIN}"
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

psql_postgres() {
  if [[ "${APPLY}" -eq 1 ]]; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 "$@"
  else
    printf 'DRY RUN: sudo -u postgres psql'
    printf ' %q' "$@"
    printf '\n'
  fi
}

restore_env() {
  if [[ -z "${BACKUP_FILE}" ]]; then
    echo "WARN: no .env backup found; skipping env restore."
    return
  fi
  [[ -f "${BACKUP_FILE}" ]] || die "Backup file not found: ${BACKUP_FILE}"
  run_or_print cp -a "${BACKUP_FILE}" "${ROOT}/.env"
}

revoke_client_db() {
  local client="$1"
  local db_name="stockwicks_${client}"
  local sql
  sql=$(cat <<SQL
REVOKE SELECT ON TABLE users FROM "${REPORT_USER}";
REVOKE SELECT ON TABLE paper_stock_trade_bots FROM "${REPORT_USER}";
REVOKE SELECT ON TABLE paper_stock_bot_live_mirror_history FROM "${REPORT_USER}";
REVOKE USAGE ON SCHEMA public FROM "${REPORT_USER}";
REVOKE CONNECT ON DATABASE "${db_name}" FROM "${REPORT_USER}";
SQL
)
  psql_postgres -d "${db_name}" -c "${sql}"
}

unset_admin_user() {
  local db_name="stockwicks_${ADMIN_CLIENT}"
  local sql="UPDATE users SET is_admin = false WHERE username = '${ADMIN_USER}';"
  psql_postgres -d "${db_name}" -c "${sql}"
}

validate_app_import() {
  if [[ -x "${ROOT}/venv/bin/python" ]]; then
    run_or_print "${ROOT}/venv/bin/python" -c "import app.main; print('rollback import ok')"
  else
    echo "WARN: ${ROOT}/venv/bin/python not found; skipping import validation."
  fi
}

restart_web() {
  [[ "${RESTART}" -eq 1 ]] || return 0
  run_or_print systemctl restart "stockwicks-${ADMIN_CLIENT}-web"
  run_or_print systemctl status --no-pager "stockwicks-${ADMIN_CLIENT}-web"
}

restore_env
if [[ "${REVOKE_GRANTS}" -eq 1 ]]; then
  for client in "${VALID_CLIENTS[@]}"; do
    revoke_client_db "${client}"
  done
fi
if [[ "${UNSET_ADMIN}" -eq 1 ]]; then
  unset_admin_user
fi
validate_app_import
restart_web

echo
if [[ "${APPLY}" -eq 1 ]]; then
  echo "Admin live-trades rollback complete."
else
  echo "Dry run complete."
fi
