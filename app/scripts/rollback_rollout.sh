#!/usr/bin/env bash
set -euo pipefail

# Roll back client folders updated by deploy_rollout.sh.
#
# Examples:
#   bash app/scripts/rollback_rollout.sh
#   sudo bash app/scripts/rollback_rollout.sh --apply --restart
#   sudo bash app/scripts/rollback_rollout.sh --apply --restart --backup /var/stockwicks/releases/haithama/rollout_backup_20260615_010203 haithama

CLIENTS_ROOT="${CLIENTS_ROOT:-/var/stockwicks/clients}"
RELEASES_ROOT="${RELEASES_ROOT:-/var/stockwicks/releases}"
APPLY=0
RESTART=0
BACKUP_DIR=""
TARGET_CLIENTS=()

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash app/scripts/rollback_rollout.sh [--apply] [--restart] [--backup <dir>] [client ...]

Defaults:
  clients: haithama yzia
  backup: latest /var/stockwicks/releases/<client>/rollout_backup_*

Options:
  --apply       Actually restore files. Without this, rsync runs dry-run.
  --restart     Restart target web/celery/beat services after restore. Requires sudo/root.
  --backup DIR  Restore a specific backup directory. Use this for one client at a time.
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
    --backup)
      BACKUP_DIR="${2:-}"
      [[ -n "${BACKUP_DIR}" ]] || die "--backup requires a directory"
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

if [[ -n "${BACKUP_DIR}" && ${#TARGET_CLIENTS[@]} -ne 1 ]]; then
  die "--backup can only be used with exactly one target client"
fi

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

echo "Targets: ${TARGET_CLIENTS[*]}"
if [[ "${APPLY}" -eq 0 ]]; then
  echo "Mode: DRY RUN. Add --apply to restore files."
else
  echo "Mode: APPLY"
fi

for client in "${TARGET_CLIENTS[@]}"; do
  [[ "${client}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "Invalid client slug: ${client}"

  target="${CLIENTS_ROOT}/${client}"
  [[ -d "${target}" ]] || die "Target client folder not found: ${target}"

  if [[ -n "${BACKUP_DIR}" ]]; then
    backup="${BACKUP_DIR}"
  else
    backup="$(find "${RELEASES_ROOT}/${client}" -maxdepth 1 -type d -name 'rollout_backup_*' 2>/dev/null | sort | tail -n 1 || true)"
  fi

  [[ -n "${backup}" && -d "${backup}" ]] || die "No rollout backup found for ${client}"

  echo
  echo "==> Rollback ${client} from ${backup}"
  rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" "${backup}/" "${target}/"

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
  echo "Dry run complete. Re-run with --apply when the listed restore looks right."
else
  echo "Rollback complete."
fi
