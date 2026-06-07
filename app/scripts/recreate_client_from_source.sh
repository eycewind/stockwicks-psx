#!/usr/bin/env bash
set -euo pipefail

# Fully recreate a commercial client from a known-good source client.
#
# This is the safest path when a client setup has drifted:
#   1. stop/remove client systemd services
#   2. remove nginx /clients/<slug>/ block and OAuth dispatcher map lines
#   3. archive the old client directory
#   4. drop the client database/user
#   5. create a fresh client from SOURCE_CLIENT, default ashakil
#
# Example:
#   sudo bash scripts/recreate_client_from_source.sh haithama 8102 2
#   sudo SOURCE_CLIENT=ashakil bash scripts/recreate_client_from_source.sh haithama 8102 2

CLIENT_SLUG="${1:-}"
APP_PORT="${2:-}"
REDIS_DB="${3:-}"
SOURCE_CLIENT="${SOURCE_CLIENT:-ashakil}"

CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "Run this script with sudo/root."
[[ -n "${CLIENT_SLUG}" && -n "${APP_PORT}" && -n "${REDIS_DB}" ]] || die "Usage: sudo bash scripts/recreate_client_from_source.sh <client_slug> <app_port> <redis_db>"
[[ "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${CLIENT_SLUG}"
[[ "${CLIENT_SLUG}" != "${SOURCE_CLIENT}" ]] || die "Refusing to recreate source client ${SOURCE_CLIENT}."
[[ -d "${CLIENTS_ROOT}/${SOURCE_CLIENT}" ]] || die "Missing source client: ${CLIENTS_ROOT}/${SOURCE_CLIENT}"

echo "Recreating ${CLIENT_SLUG} from ${SOURCE_CLIENT}"
echo "Port: ${APP_PORT}"
echo "Redis DB: ${REDIS_DB}"
echo

bash "${SCRIPT_DIR}/delete_client.sh" "${CLIENT_SLUG}" --yes --drop-db

SOURCE_CLIENT="${SOURCE_CLIENT}" bash "${SCRIPT_DIR}/setup_new_client.sh" "${CLIENT_SLUG}" "${APP_PORT}" "${REDIS_DB}"

echo
echo "Recreate complete for ${CLIENT_SLUG}"
echo "URL: https://www.stockwicks.com/clients/${CLIENT_SLUG}"
echo
echo "Check status:"
echo "  sudo bash ${SCRIPT_DIR}/restart_client_services.sh ${CLIENT_SLUG} --logs"
