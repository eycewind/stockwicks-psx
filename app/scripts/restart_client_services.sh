#!/usr/bin/env bash
set -euo pipefail

# Restart all StockWicks services for one commercial client.
#
# Example:
#   sudo bash scripts/restart_client_services.sh ahaitham

CLIENT_SLUG="${1:-}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
  die "Run this script with sudo/root."
fi

if [[ -z "${CLIENT_SLUG}" ]]; then
  die "Usage: sudo bash scripts/restart_client_services.sh <client_slug>"
fi

if [[ ! "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  die "Invalid client slug: ${CLIENT_SLUG}"
fi

SERVICES=(
  "stockwicks-${CLIENT_SLUG}-web"
  "stockwicks-${CLIENT_SLUG}-celery"
  "stockwicks-${CLIENT_SLUG}-beat"
)

systemctl daemon-reload
systemctl restart "${SERVICES[@]}"
systemctl status --no-pager "${SERVICES[@]}"
