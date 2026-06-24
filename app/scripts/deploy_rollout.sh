#!/usr/bin/env bash
set -euo pipefail

# Roll out a release folder to one or more deployed client folders.
#
# Default is a dry run. Use --apply to actually copy files.
#
# Examples from a packaged release folder or a client root:
#   bash app/scripts/deploy_rollout.sh
#   bash app/scripts/deploy_rollout.sh --apply
#   sudo bash app/scripts/deploy_rollout.sh --apply --restart
#   bash app/scripts/deploy_rollout.sh --source /tmp/stockwicks-release --all
#   bash app/scripts/deploy_rollout.sh --source /tmp/stockwicks-release client_a client_b

CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
RELEASES_ROOT="${RELEASES_ROOT:-/var/stockwicks/releases}"
SOURCE_ROOT=""
APPLY=0
RESTART=0
TARGET_ALL=0
TARGET_CLIENTS=()

REQUIRED_FILES=(
  "app/main.py"
  "app/celery_worker.py"
  "app/database/connection.py"
  "app/models/__init__.py"
  "app/models/user.py"
)

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash app/scripts/deploy_rollout.sh [--apply] [--restart] [--source <path>] [--all|client ...]

Defaults:
  source: current directory if it looks like a StockWicks release/client root
  clients: all client folders under CLIENTS_ROOT

Options:
  --apply       Actually sync files. Without this, rsync runs dry-run.
  --restart     Restart target web/celery/beat services after sync. Requires sudo/root.
  --source PATH Source client root to copy from.
  --all         Discover and update every client folder under CLIENTS_ROOT.
  -h, --help    Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --restart)
      RESTART=1
      shift
      ;;
    --source)
      SOURCE_ROOT="${2:-}"
      [[ -n "${SOURCE_ROOT}" ]] || die "--source requires a path"
      shift 2
      ;;
    --all)
      TARGET_ALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      die "Unknown option: $1"
      ;;
    *)
      TARGET_CLIENTS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "${SOURCE_ROOT}" ]]; then
  if [[ -d "app" && -f "app/main.py" ]]; then
    SOURCE_ROOT="$(pwd)"
  else
    die "--source is required unless you run from a StockWicks release/client root"
  fi
fi

SOURCE_ROOT="$(cd "${SOURCE_ROOT}" && pwd)"
SOURCE_SLUG="$(basename "${SOURCE_ROOT}")"

[[ -d "${SOURCE_ROOT}/app" ]] || die "Source does not look like a StockWicks client root: ${SOURCE_ROOT}"
command -v rsync >/dev/null 2>&1 || die "rsync is required. Install with: sudo apt install -y rsync"
for required_file in "${REQUIRED_FILES[@]}"; do
  [[ -f "${SOURCE_ROOT}/${required_file}" ]] || die "Source is missing required file: ${SOURCE_ROOT}/${required_file}"
done

RSYNC_FLAGS=(-avc --delete)
if [[ "${APPLY}" -eq 0 ]]; then
  RSYNC_FLAGS+=(-n)
fi

EXCLUDES=(
  --exclude ".git/"
  --exclude ".env"
  --exclude "data/"
  --exclude "logs/"
  --exclude "venv/"
  --exclude "__pycache__/"
  --exclude "*.pyc"
  --exclude "celerybeat-schedule*"
)

BACKUP_EXCLUDES=(
  --exclude ".git/"
  --exclude ".env"
  --exclude "data/"
  --exclude "logs/"
  --exclude "venv/"
  --exclude "__pycache__/"
  --exclude "*.pyc"
  --exclude "celerybeat-schedule*"
)

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

discover_clients() {
  local found=()
  local dir
  while IFS= read -r -d '' dir; do
    [[ -f "${dir}/app/main.py" ]] || continue
    found+=("$(basename "${dir}")")
  done < <(find "${CLIENTS_ROOT}" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
  printf '%s\n' "${found[@]}"
}

if [[ "${TARGET_ALL}" -eq 1 || ${#TARGET_CLIENTS[@]} -eq 0 ]]; then
  mapfile -t TARGET_CLIENTS < <(discover_clients)
fi

[[ ${#TARGET_CLIENTS[@]} -gt 0 ]] || die "No target clients found under ${CLIENTS_ROOT}"

echo "Source: ${SOURCE_ROOT}"
echo "Targets: ${TARGET_CLIENTS[*]}"
if [[ "${APPLY}" -eq 0 ]]; then
  echo "Mode: DRY RUN. Add --apply to copy files."
else
  echo "Mode: APPLY"
fi

for client in "${TARGET_CLIENTS[@]}"; do
  [[ "${client}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${client}"

  target="${CLIENTS_ROOT}/${client}"
  [[ -d "${target}" ]] || die "Target client folder not found: ${target}"
  target_real="$(cd "${target}" && pwd)"
  [[ "${target_real}" != "${SOURCE_ROOT}" ]] || die "Target client matches source path: ${client}"

  echo
  echo "==> Sync ${SOURCE_SLUG} -> ${client}"
  if [[ "${APPLY}" -eq 1 ]]; then
    backup_dir="${RELEASES_ROOT}/${client}/rollout_backup_${TIMESTAMP}"
    echo "==> Backup ${client} -> ${backup_dir}"
    mkdir -p "${backup_dir}"
    rsync -a "${BACKUP_EXCLUDES[@]}" "${target}/" "${backup_dir}/"
  fi

  rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" "${SOURCE_ROOT}/" "${target}/"

  if [[ "${APPLY}" -eq 1 ]]; then
    for required_file in "${REQUIRED_FILES[@]}"; do
      [[ -f "${target}/${required_file}" ]] || die "Target ${client} is missing required file after rsync: ${target}/${required_file}"
    done

    if [[ -x "${target}/venv/bin/python" ]]; then
      echo "==> Validate ${client} imports"
      (
        cd "${target}"
        "${target}/venv/bin/python" -c "import app.main; print('web import ok')"
        "${target}/venv/bin/python" -c "from app.celery_worker import celery; print('celery import ok')"
      )
    else
      die "Target ${client} is missing executable venv python: ${target}/venv/bin/python"
    fi
  fi

  if [[ "${APPLY}" -eq 1 && "${RESTART}" -eq 1 ]]; then
    [[ "${EUID}" -eq 0 ]] || die "--restart requires sudo/root"
    services=(
      "stockwicks-${client}-web"
      "stockwicks-${client}-celery"
      "stockwicks-${client}-beat"
    )
    echo "==> Restart ${client} services"
    systemctl daemon-reload
    systemctl restart "${services[@]}"
    systemctl status --no-pager "${services[@]}"
  fi
done

echo
if [[ "${APPLY}" -eq 0 ]]; then
  echo "Dry run complete. Re-run with --apply when the listed changes look right."
else
  echo "Rollout complete."
fi
