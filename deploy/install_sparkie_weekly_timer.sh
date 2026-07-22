#!/usr/bin/env bash
set -euo pipefail

CLIENT_SLUG="${1:-${CLIENT_SLUG:-ashakil}}"
TARGET_ROOT="${2:-/var/stockwicks/clients/${CLIENT_SLUG}}"
SERVICE="stockwicks-${CLIENT_SLUG}-sparkie-weekly.service"
TIMER="stockwicks-${CLIENT_SLUG}-sparkie-weekly.timer"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo/root." >&2
  exit 1
fi

cat > "/etc/systemd/system/${SERVICE}" <<EOF
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

cat > "/etc/systemd/system/${TIMER}" <<EOF
[Unit]
Description=Run StockWicks ${CLIENT_SLUG} Weekly Sparkie every Sunday

[Timer]
OnCalendar=Sun *-*-* 02:00:00 America/New_York
Persistent=true
Unit=${SERVICE}

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${TIMER}"
systemctl status --no-pager "${TIMER}"
