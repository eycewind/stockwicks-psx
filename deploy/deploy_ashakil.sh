#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${ASHAKIL_APP_PATH:-/var/stockwicks/clients/ashakil}"
RELEASE_SRC="${RELEASE_SRC:-}"
RELEASES_DIR="${RELEASES_DIR:-/var/stockwicks/releases/ashakil}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${RELEASES_DIR}/backup_${TIMESTAMP}"

REQUIRED_FILES=(
  "app/main.py"
  "app/celery_worker.py"
  "app/database/connection.py"
  "app/models/__init__.py"
  "app/models/user.py"
)

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
mkdir -p "${APP_PATH}/models"
chmod 2775 "${APP_PATH}/models" || true

echo "Backing up current app to ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
rsync -a \
  --exclude 'venv' \
  --exclude '.env' \
  --exclude 'data' \
  --exclude 'logs' \
  --exclude 'models' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "${APP_PATH}/" "${BACKUP_DIR}/"

echo "Deploying app release from ${RELEASE_SRC}/app to ${APP_PATH}/app"
if [[ ! -d "${RELEASE_SRC}/app" ]]; then
  echo "ERROR: release app directory missing: ${RELEASE_SRC}/app"
  exit 1
fi
for required_file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "${RELEASE_SRC}/${required_file}" ]]; then
    echo "ERROR: release missing required file: ${RELEASE_SRC}/${required_file}"
    exit 1
  fi
done

rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'cache' \
  --exclude 'data' \
  --exclude 'logs' \
  "${RELEASE_SRC}/app/" "${APP_PATH}/app/"

for required_file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "${APP_PATH}/${required_file}" ]]; then
    echo "ERROR: deployed app missing required file: ${APP_PATH}/${required_file}"
    exit 1
  fi
done

if [[ ! -x "${APP_PATH}/venv/bin/python" ]]; then
  echo "ERROR: venv python is missing or not executable: ${APP_PATH}/venv/bin/python"
  exit 1
fi

echo "Validating ashakil imports"
(
  cd "${APP_PATH}"
  "${APP_PATH}/venv/bin/python" -c "import app.main; print('web import ok')"
  "${APP_PATH}/venv/bin/python" -c "from app.celery_worker import celery; print('celery import ok')"
)

echo "Restarting ashakil services"
sudo systemctl daemon-reload
sudo systemctl restart stockwicks-ashakil-web stockwicks-ashakil-celery stockwicks-ashakil-beat

echo "Checking service status"
sudo systemctl is-active stockwicks-ashakil-web
sudo systemctl is-active stockwicks-ashakil-celery
sudo systemctl is-active stockwicks-ashakil-beat

echo "Checking local web health"
for attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8101/healthz; then
    echo
    echo "Local web health ok"
    break
  fi
  if [[ "${attempt}" -eq 30 ]]; then
    echo "ERROR: local web health failed after ${attempt} attempts"
    sudo systemctl status --no-pager stockwicks-ashakil-web || true
    sudo journalctl -u stockwicks-ashakil-web -n 80 --no-pager || true
    exit 1
  fi
  sleep 1
done

echo "Deploy complete: ${TIMESTAMP}"
