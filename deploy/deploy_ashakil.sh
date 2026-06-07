#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${ASHAKIL_APP_PATH:-/var/stockwicks/clients/ashakil}"
RELEASE_SRC="${RELEASE_SRC:-}"
RELEASES_DIR="${RELEASES_DIR:-/var/stockwicks/releases/ashakil}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${RELEASES_DIR}/backup_${TIMESTAMP}"

if [[ -z "${RELEASE_SRC}" ]]; then
  echo "ERROR: RELEASE_SRC is required."
  exit 1
fi

if [[ ! -d "${RELEASE_SRC}" ]]; then
  echo "ERROR: RELEASE_SRC does not exist: ${RELEASE_SRC}"
  exit 1
fi

if [[ ! -d "${APP_PATH}" ]]; then
  echo "ERROR: APP_PATH does not exist: ${APP_PATH}"
  exit 1
fi

mkdir -p "${RELEASES_DIR}"

echo "Backing up current app to ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
rsync -a \
  --exclude 'venv' \
  --exclude '.env' \
  --exclude 'data' \
  --exclude 'logs' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "${APP_PATH}/" "${BACKUP_DIR}/"

echo "Deploying release from ${RELEASE_SRC} to ${APP_PATH}"
rsync -a --delete \
  --exclude '.env' \
  --exclude 'venv' \
  --exclude 'data' \
  --exclude 'logs' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "${RELEASE_SRC}/" "${APP_PATH}/"

echo "Restarting ashakil services"
sudo systemctl daemon-reload
sudo systemctl restart stockwicks-ashakil-web stockwicks-ashakil-celery stockwicks-ashakil-beat

echo "Checking service status"
sudo systemctl is-active stockwicks-ashakil-web
sudo systemctl is-active stockwicks-ashakil-celery
sudo systemctl is-active stockwicks-ashakil-beat

echo "Deploy complete: ${TIMESTAMP}"