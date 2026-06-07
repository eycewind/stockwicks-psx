#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${ASHAKIL_APP_PATH:-/var/stockwicks/clients/ashakil}"
RELEASES_DIR="${RELEASES_DIR:-/var/stockwicks/releases/ashakil}"
BACKUP_DIR="${1:-}"

if [[ -z "${BACKUP_DIR}" ]]; then
  BACKUP_DIR="$(find "${RELEASES_DIR}" -maxdepth 1 -type d -name 'backup_*' | sort | tail -n 1)"
fi

if [[ -z "${BACKUP_DIR}" || ! -d "${BACKUP_DIR}" ]]; then
  echo "ERROR: No backup directory found."
  exit 1
fi

if [[ ! -d "${APP_PATH}" ]]; then
  echo "ERROR: APP_PATH does not exist: ${APP_PATH}"
  exit 1
fi

echo "Rolling back ${APP_PATH} from ${BACKUP_DIR}"

rsync -a --delete \
  --exclude '.env' \
  --exclude 'venv' \
  --exclude 'data' \
  --exclude 'logs' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "${BACKUP_DIR}/" "${APP_PATH}/"

echo "Restarting ashakil services"
sudo systemctl daemon-reload
sudo systemctl restart stockwicks-ashakil-web stockwicks-ashakil-celery stockwicks-ashakil-beat

echo "Checking service status"
sudo systemctl is-active stockwicks-ashakil-web
sudo systemctl is-active stockwicks-ashakil-celery
sudo systemctl is-active stockwicks-ashakil-beat

echo "Rollback complete from ${BACKUP_DIR}"