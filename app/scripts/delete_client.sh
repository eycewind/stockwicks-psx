#!/usr/bin/env bash
set -euo pipefail

# Remove a StockWicks commercial client from systemd/nginx and archive its files.
#
# Conservative default:
# - stops/disables services
# - removes generated systemd service files
# - backs up nginx before editing
# - archives the client directory instead of deleting it
# - keeps database/user unless --drop-db is passed
#
# Example:
#   sudo bash scripts/delete_client.sh haithama --yes
#   sudo bash scripts/delete_client.sh haithama --yes --drop-db

CLIENT_SLUG="${1:-}"
CONFIRM="${2:-}"
DROP_DB="${3:-}"

CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
CLIENT_ROOT="${CLIENTS_ROOT}/${CLIENT_SLUG}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-enabled/stonxs}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
ARCHIVE_ROOT="${ARCHIVE_ROOT:-/var/stockwicks/deleted_clients}"
DELETE_LOG_DIR="${DELETE_LOG_DIR:-/var/stockwicks/client_setup_logs}"
NGINX_BACKUP_DIR="${NGINX_BACKUP_DIR:-/var/stockwicks/nginx_backups}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DELETE_LOG="${DELETE_LOG_DIR}/delete_${CLIENT_SLUG:-unknown}_${TIMESTAMP}.log"

DB_NAME="stockwicks_${CLIENT_SLUG}"
DB_USER="stockwicks_${CLIENT_SLUG}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

start_logging() {
  mkdir -p "${DELETE_LOG_DIR}"
  exec > >(tee -a "${DELETE_LOG}") 2>&1
  echo "StockWicks client delete log"
  echo "Started: $(date -Is)"
  echo "Client: ${CLIENT_SLUG:-}"
  echo "Client root: ${CLIENT_ROOT}"
  echo "Drop DB: ${DROP_DB}"
  echo "Log file: ${DELETE_LOG}"
  echo
}

validate_inputs() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script with sudo/root."
  [[ -n "${CLIENT_SLUG}" ]] || die "Usage: sudo bash scripts/delete_client.sh <client_slug> --yes [--drop-db]"
  [[ "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${CLIENT_SLUG}"
  [[ "${CLIENT_SLUG}" != "ashakil" ]] || die "Refusing to delete source client ashakil."
  [[ "${CONFIRM}" == "--yes" ]] || die "Refusing to delete without --yes."
  [[ -z "${DROP_DB}" || "${DROP_DB}" == "--drop-db" ]] || die "Unknown option: ${DROP_DB}"
}

backup_common_file() {
  local file="$1"
  if [[ -e "${file}" ]]; then
    mkdir -p "${NGINX_BACKUP_DIR}"
    local backup="${NGINX_BACKUP_DIR}/$(basename "${file}").bak.delete_${CLIENT_SLUG}.${TIMESTAMP}"
    cp -a "${file}" "${backup}"
    echo "Backed up ${file} -> ${backup}"
  fi
}

stop_services() {
  local services=(
    "stockwicks-${CLIENT_SLUG}-web"
    "stockwicks-${CLIENT_SLUG}-celery"
    "stockwicks-${CLIENT_SLUG}-beat"
  )

  for svc in "${services[@]}"; do
    if systemctl list-unit-files "${svc}.service" --no-legend | grep -q "${svc}.service"; then
      systemctl disable --now "${svc}" || true
    else
      echo "Service not found, skipping: ${svc}"
    fi

    if [[ -f "${SYSTEMD_DIR}/${svc}.service" ]]; then
      rm -f "${SYSTEMD_DIR}/${svc}.service"
      echo "Removed ${SYSTEMD_DIR}/${svc}.service"
    fi
  done

  systemctl daemon-reload
}

remove_nginx_client_block() {
  [[ -f "${NGINX_SITE}" ]] || die "Missing nginx site file: ${NGINX_SITE}"
  backup_common_file "${NGINX_SITE}"

  python3 - "$NGINX_SITE" "$CLIENT_SLUG" <<'PY'
from pathlib import Path
import re
import sys

site = Path(sys.argv[1])
client = sys.argv[2]
text = site.read_text()

pattern = re.compile(
    rf"\n\s*# ============================\n"
    rf"\s*# Commercial client: {re.escape(client)}\n"
    rf"\s*# ============================\n"
    rf"\s*location /clients/{re.escape(client)}/ \{{.*?\n\s*\}}\n",
    re.DOTALL,
)

new_text, count = pattern.subn("\n", text, count=1)
if count:
    print(f"Removed nginx /clients/{client}/ block")
else:
    print(f"No generated nginx /clients/{client}/ block found")

# Remove OAuth dispatcher map lines for this client. This is critical before
# recreating a client on a different port because nginx map order can otherwise
# route callbacks to an old service.
new_text = re.sub(
    rf"^\s*~\^{re.escape(client)}(?:\(:\|%3A\)|:)\s+127\.0\.0\.1:\d+;\n",
    "",
    new_text,
    flags=re.M,
)
new_text = re.sub(
    rf"^\s*~\^{re.escape(client)}(?:\(:\|%3A\)|:)\s+/clients/{re.escape(client)};\n",
    "",
    new_text,
    flags=re.M,
)

site.write_text(new_text)
print(f"Removed nginx OAuth dispatcher map entries for {client}")
PY

  nginx -t
  systemctl reload nginx
}

archive_client_root() {
  if [[ ! -e "${CLIENT_ROOT}" ]]; then
    echo "Client root not found, skipping archive: ${CLIENT_ROOT}"
    return
  fi

  mkdir -p "${ARCHIVE_ROOT}"
  local archive_path="${ARCHIVE_ROOT}/${CLIENT_SLUG}_${TIMESTAMP}"
  mv "${CLIENT_ROOT}" "${archive_path}"
  echo "Archived ${CLIENT_ROOT} -> ${archive_path}"
}

drop_database_if_requested() {
  if [[ "${DROP_DB}" != "--drop-db" ]]; then
    echo "Keeping DB/user. Pass --drop-db to remove ${DB_NAME} and ${DB_USER}."
    return
  fi

  sudo -u postgres dropdb --if-exists "${DB_NAME}"
  sudo -u postgres dropuser --if-exists "${DB_USER}"
  echo "Dropped DB/user: ${DB_NAME}, ${DB_USER}"
}

main() {
  start_logging
  validate_inputs
  stop_services
  remove_nginx_client_block
  archive_client_root
  drop_database_if_requested

  echo
  echo "Deleted client setup for ${CLIENT_SLUG}"
  echo "Delete log: ${DELETE_LOG}"
}

main "$@"
