#!/usr/bin/env bash
set -euo pipefail

# Set SCHWAB_REFRESH_USER_ID for a client after the real user signs up.
#
# Example:
#   sudo bash scripts/set_client_refresh_user.sh haithama user@example.com
#   sudo bash scripts/set_client_refresh_user.sh haithama 7

CLIENT_SLUG="${1:-}"
USER_LOOKUP="${2:-}"
CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
CLIENT_ROOT="${CLIENTS_ROOT}/${CLIENT_SLUG}"
ENV_FILE="${CLIENT_ROOT}/.env"
DB_NAME="stockwicks_${CLIENT_SLUG}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || die "Run this script with sudo/root."
[[ -n "${CLIENT_SLUG}" && -n "${USER_LOOKUP}" ]] || die "Usage: sudo bash scripts/set_client_refresh_user.sh <client_slug> <email_or_user_id>"
[[ "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${CLIENT_SLUG}"
[[ -f "${ENV_FILE}" ]] || die "Missing env file: ${ENV_FILE}"

if [[ "${USER_LOOKUP}" =~ ^[0-9]+$ ]]; then
  USER_ID="${USER_LOOKUP}"
else
  USER_ID="$(
    sudo -u postgres psql -d "${DB_NAME}" -v lookup="${USER_LOOKUP}" -tA <<'SQL'
SELECT id
FROM users
WHERE lower(email) = lower(:'lookup')
   OR lower(username) = lower(:'lookup')
ORDER BY id DESC
LIMIT 1;
SQL
  )"
fi

[[ -n "${USER_ID}" ]] || die "Could not find user in ${DB_NAME}: ${USER_LOOKUP}"
[[ "${USER_ID}" =~ ^[0-9]+$ ]] || die "Invalid user id found: ${USER_ID}"

cp -a "${ENV_FILE}" "${ENV_FILE}.bak.refresh_user.$(date +%Y%m%d_%H%M%S)"

if grep -q '^SCHWAB_REFRESH_USER_ID=' "${ENV_FILE}"; then
  sed -i "s/^SCHWAB_REFRESH_USER_ID=.*/SCHWAB_REFRESH_USER_ID=${USER_ID}/" "${ENV_FILE}"
else
  echo "SCHWAB_REFRESH_USER_ID=${USER_ID}" >> "${ENV_FILE}"
fi

mkdir -p "${CLIENT_ROOT}/data/${USER_ID}"
chown -R www-data:www-data "${CLIENT_ROOT}/data/${USER_ID}"
chmod 770 "${CLIENT_ROOT}/data/${USER_ID}"

systemctl restart "stockwicks-${CLIENT_SLUG}-web" "stockwicks-${CLIENT_SLUG}-celery" "stockwicks-${CLIENT_SLUG}-beat"

echo "Set ${CLIENT_SLUG} SCHWAB_REFRESH_USER_ID=${USER_ID}"
echo "Token folder: ${CLIENT_ROOT}/data/${USER_ID}"
