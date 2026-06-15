#!/usr/bin/env bash
set -euo pipefail

# Roll out code from one deployed client folder to other client folders.
#
# Default is a dry run. Use --apply to actually copy files.
#
# Examples from /var/stockwicks/clients/ashakil:
#   bash app/scripts/deploy_rollout.sh
#   bash app/scripts/deploy_rollout.sh --apply
#   sudo bash app/scripts/deploy_rollout.sh --apply --restart
#   bash app/scripts/deploy_rollout.sh --source /var/stockwicks/clients/ashakil haithama yzia

CLIENTS_ROOT_DEFAULT="/var/stockwicks/clients"
SOURCE_ROOT=""
APPLY=0
RESTART=0
TARGET_CLIENTS=()

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash app/scripts/deploy_rollout.sh [--apply] [--restart] [--source <path>] [client ...]

Defaults:
  source: current client root if run from a client folder, otherwise /var/stockwicks/clients/ashakil
  clients: haithama yzia

Options:
  --apply       Actually sync files. Without this, rsync runs dry-run.
  --restart     Restart target web/celery/beat services after sync. Requires sudo/root.
  --source PATH Source client root to copy from.
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

if [[ ${#TARGET_CLIENTS[@]} -eq 0 ]]; then
  TARGET_CLIENTS=("haithama" "yzia")
fi

if [[ -z "${SOURCE_ROOT}" ]]; then
  if [[ -d "app" && -f "app/main.py" ]]; then
    SOURCE_ROOT="$(pwd)"
  else
    SOURCE_ROOT="${CLIENTS_ROOT_DEFAULT}/ashakil"
  fi
fi

SOURCE_ROOT="$(cd "${SOURCE_ROOT}" && pwd)"
CLIENTS_ROOT="$(dirname "${SOURCE_ROOT}")"
SOURCE_SLUG="$(basename "${SOURCE_ROOT}")"

[[ -d "${SOURCE_ROOT}/app" ]] || die "Source does not look like a StockWicks client root: ${SOURCE_ROOT}"
command -v rsync >/dev/null 2>&1 || die "rsync is required. Install with: sudo apt install -y rsync"

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

echo "Source: ${SOURCE_ROOT}"
echo "Targets: ${TARGET_CLIENTS[*]}"
if [[ "${APPLY}" -eq 0 ]]; then
  echo "Mode: DRY RUN. Add --apply to copy files."
else
  echo "Mode: APPLY"
fi

for client in "${TARGET_CLIENTS[@]}"; do
  [[ "${client}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${client}"
  [[ "${client}" != "${SOURCE_SLUG}" ]] || die "Target client matches source: ${client}"

  target="${CLIENTS_ROOT}/${client}"
  [[ -d "${target}" ]] || die "Target client folder not found: ${target}"

  echo
  echo "==> Sync ${SOURCE_SLUG} -> ${client}"
  rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" "${SOURCE_ROOT}/" "${target}/"

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
