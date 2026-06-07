#!/usr/bin/env bash
set -euo pipefail

SERVICES=(
  stockwicks-ashakil-web
  stockwicks-ashakil-celery
  stockwicks-ashakil-beat
)

echo "Restarting ashakil services..."
for svc in "${SERVICES[@]}"; do
  if systemctl list-unit-files | grep -q "^${svc}.service"; then
    echo "Restarting ${svc}"
    sudo systemctl restart "${svc}"
  else
    echo "SKIP: ${svc} not found"
  fi
done

echo
echo "Service status:"
for svc in "${SERVICES[@]}"; do
  if systemctl list-unit-files | grep -q "^${svc}.service"; then
    echo "===== ${svc} ====="
    systemctl --no-pager --full status "${svc}" | sed -n '1,18p'
    echo
  fi
done

echo
echo "Recent logs:"
for svc in "${SERVICES[@]}"; do
  if systemctl list-unit-files | grep -q "^${svc}.service"; then
    echo "===== logs: ${svc} ====="
    journalctl -u "${svc}" --no-pager -n 80
    echo
  fi
done
