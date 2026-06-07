#!/usr/bin/env bash
set -euo pipefail

# Check URL, service status, recent logs, and token folders for one client.
#
# Example:
#   sudo bash scripts/check_client_status.sh haithama

CLIENT_SLUG="${1:-}"
CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
CLIENT_ROOT="${CLIENTS_ROOT}/${CLIENT_SLUG}"
PUBLIC_URL="${PUBLIC_URL:-https://www.stockwicks.com/clients/${CLIENT_SLUG}/}"
LINES="${LINES:-80}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ -n "${CLIENT_SLUG}" ]] || die "Usage: sudo bash scripts/check_client_status.sh <client_slug>"
[[ "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${CLIENT_SLUG}"

echo "== Client =="
echo "Slug: ${CLIENT_SLUG}"
echo "Root: ${CLIENT_ROOT}"
echo "URL: ${PUBLIC_URL}"
echo

echo "== HTTP Headers =="
curl -I "${PUBLIC_URL}" || true
echo

echo "== Services =="
systemctl status --no-pager \
  "stockwicks-${CLIENT_SLUG}-web" \
  "stockwicks-${CLIENT_SLUG}-celery" \
  "stockwicks-${CLIENT_SLUG}-beat" || true
echo

echo "== Web Logs =="
journalctl -u "stockwicks-${CLIENT_SLUG}-web" -n "${LINES}" --no-pager || true
echo

echo "== Celery Logs =="
journalctl -u "stockwicks-${CLIENT_SLUG}-celery" -n "${LINES}" --no-pager || true
echo

echo "== Beat Logs =="
journalctl -u "stockwicks-${CLIENT_SLUG}-beat" -n "${LINES}" --no-pager || true
echo

echo "== Client Data Users =="
if [[ -d "${CLIENT_ROOT}/data" ]]; then
  find "${CLIENT_ROOT}/data" -maxdepth 2 -type f -name '*token*.json' -printf '%p\n' 2>/dev/null || true
  find "${CLIENT_ROOT}/data" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null || true
else
  echo "No data directory found: ${CLIENT_ROOT}/data"
fi
