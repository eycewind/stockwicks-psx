#!/usr/bin/env bash
set -euo pipefail

# Install/update nginx routing for shared Schwab OAuth callbacks.
#
# The Schwab callback URL stays shared, for example:
#   https://www.stockwicks.com/auth/callback
#   https://www.stockwicks.com/auth/schwab/db/callback
#
# nginx routes the callback to the correct client app by OAuth state prefix:
#   haithama:trade:1:... -> 127.0.0.1:8102
#
# Example:
#   sudo bash scripts/install_oauth_dispatcher.sh haithama 8102

CLIENT_SLUG="${1:-}"
APP_PORT="${2:-}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-enabled/stonxs}"
LOG_DIR="${LOG_DIR:-/var/stockwicks/client_setup_logs}"
NGINX_BACKUP_DIR="${NGINX_BACKUP_DIR:-/var/stockwicks/nginx_backups}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/oauth_dispatcher_${CLIENT_SLUG:-unknown}_${TIMESTAMP}.log"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

start_logging() {
  mkdir -p "${LOG_DIR}"
  exec > >(tee -a "${LOG_FILE}") 2>&1
  echo "StockWicks OAuth dispatcher install"
  echo "Started: $(date -Is)"
  echo "Client: ${CLIENT_SLUG}"
  echo "Port: ${APP_PORT}"
  echo "Nginx site: ${NGINX_SITE}"
  echo "Log file: ${LOG_FILE}"
  echo
}

validate_inputs() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script with sudo/root."
  [[ -n "${CLIENT_SLUG}" && -n "${APP_PORT}" ]] || die "Usage: sudo bash scripts/install_oauth_dispatcher.sh <client_slug> <app_port>"
  [[ "${CLIENT_SLUG}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${CLIENT_SLUG}"
  [[ "${APP_PORT}" =~ ^[0-9]+$ ]] || die "APP_PORT must be numeric."
  [[ -f "${NGINX_SITE}" ]] || die "Missing nginx site: ${NGINX_SITE}"
}

patch_nginx() {
  mkdir -p "${NGINX_BACKUP_DIR}"
  local backup="${NGINX_BACKUP_DIR}/$(basename "${NGINX_SITE}").bak.oauth_dispatcher_${CLIENT_SLUG}.${TIMESTAMP}"
  cp -a "${NGINX_SITE}" "${backup}"
  echo "Backed up ${NGINX_SITE} -> ${backup}"

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
    default 127.0.0.1:8512;
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
        raise SystemExit("Could not find first nginx server block.")
    text = text[:first_server] + maps + text[first_server:]
else:
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

def replace_exact_location(src: str, location_name: str, replacement: str) -> tuple[str, int]:
    pattern = re.compile(rf"location = {re.escape(location_name)} \{{.*?\n\s*\}}", re.DOTALL)
    return pattern.subn(replacement, src)

text, callback_count = replace_exact_location(text, "/auth/callback", callback_block)
text, trade_count = replace_exact_location(text, "/auth/schwab/db/callback", trade_block)

site.write_text(text)
print(f"Updated {callback_count} /auth/callback block(s)")
print(f"Updated {trade_count} /auth/schwab/db/callback block(s)")
PY
}

main() {
  start_logging
  validate_inputs
  patch_nginx
  nginx -t
  systemctl reload nginx
  echo
  echo "OAuth dispatcher installed for ${CLIENT_SLUG} -> 127.0.0.1:${APP_PORT}"
  echo "Log: ${LOG_FILE}"
}

main "$@"
