#!/usr/bin/env bash
# Daily SQLite backup: online snapshot -> integrity check -> gzip -> local dir -> optional rclone.
#
#   scripts/ops/backup.sh [--dry-run] [--label NAME]
#
# The snapshot is taken by `python -m app.ops.sqlite_tool backup` (sqlite3 online backup API,
# the same mechanism as the CLI `.backup`), run in the application image so the host needs
# neither sqlite3 nor Python. The tool verifies the copy with PRAGMA integrity_check before
# compressing, so a corrupt snapshot fails the job instead of becoming "a backup".
#
# Environment (all optional):
#   APP_DIR          deployment directory                (default: /opt/ai-tahlilchi)
#   BACKUP_DIR       where backups are kept              (default: $APP_DIR/backups)
#   RETENTION_DAYS   local and remote retention          (default: 14)
#   RCLONE_REMOTE    e.g. "b2:my-bucket/ai-tahlilchi"; unset = local only
#   COMPOSE          compose command                     (default: docker compose with env files)
#   TOOL_CMD         command that runs the sqlite tool   (default: compose run ... sqlite_tool)
#                    (tests override it to call the tool on the host without Docker)
#   DB_PATH          database path as the tool sees it   (default: /data/app.db)
#   BACKUP_PING_URL  healthchecks.io URL to ping when the backup succeeded (/fail on failure)
#
# Exit codes: 0 ok, 1 backup failed, 3 backup made but the remote copy failed.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-tahlilchi}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
DB_PATH="${DB_PATH:-/data/app.db}"
BACKUP_PING_URL="${BACKUP_PING_URL:-}"
DRY_RUN=0
LABEL="daily"

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; ping_monitor /fail; exit "${2:-1}"; }

ping_monitor() {
    [ -n "$BACKUP_PING_URL" ] || return 0
    curl -fsS -m 10 --retry 2 -o /dev/null "${BACKUP_PING_URL}${1:-}" || log "monitor ping failed (ignored)"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --label) shift; LABEL="${1:?--label needs a value}" ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
    shift
done

case "$RETENTION_DAYS" in ''|*[!0-9]*) echo "RETENTION_DAYS must be a number" >&2; exit 64 ;; esac
case "$LABEL" in *[!A-Za-z0-9_-]*|'') echo "--label may contain letters, digits, - and _ only" >&2; exit 64 ;; esac

if [ -z "${COMPOSE:-}" ]; then
    COMPOSE="docker compose --project-directory $APP_DIR -f $APP_DIR/docker-compose.yml --env-file $APP_DIR/.env"
    [ -f "$APP_DIR/deploy.env" ] && COMPOSE="$COMPOSE --env-file $APP_DIR/deploy.env"
fi
TOOL_CMD="${TOOL_CMD:-$COMPOSE run --rm --no-deps -T --entrypoint python bot -m app.ops.sqlite_tool}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$BACKUP_DIR/app-$stamp-$LABEL.db.gz"
partial="$final.part"

if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] would write $final via: $TOOL_CMD backup --db $DB_PATH --output -"
    [ -z "$RCLONE_REMOTE" ] || log "[dry-run] would copy to $RCLONE_REMOTE and prune remote files older than ${RETENTION_DAYS}d"
    log "[dry-run] would delete local backups older than ${RETENTION_DAYS} days"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
trap 'rm -f "$partial"' EXIT

# shellcheck disable=SC2086  # TOOL_CMD is a command line made of several words on purpose
if ! $TOOL_CMD backup --db "$DB_PATH" --output - > "$partial"; then
    die "snapshot failed (see messages above)"
fi
[ -s "$partial" ] || die "snapshot is empty"

# Second, independent check of the finished file: gzip stream complete and database intact.
# shellcheck disable=SC2086
if ! $TOOL_CMD verify --input - < "$partial"; then
    die "the finished backup does not verify"
fi

chmod 600 "$partial"
mv "$partial" "$final"
log "wrote $final ($(wc -c < "$final") bytes)"

# Retention: local first, so a remote outage never fills the disk.
find "$BACKUP_DIR" -maxdepth 1 -name 'app-*.db.gz' -mtime +"$RETENTION_DAYS" -print -delete \
    | sed 's/^/backup: pruned /' >&2 || true

if [ -n "$RCLONE_REMOTE" ]; then
    if ! command -v rclone >/dev/null 2>&1; then
        die "RCLONE_REMOTE is set but rclone is not installed" 3
    fi
    if ! rclone copy "$final" "$RCLONE_REMOTE" --quiet; then
        die "rclone copy to $RCLONE_REMOTE failed; the local backup is kept" 3
    fi
    rclone delete "$RCLONE_REMOTE" --min-age "${RETENTION_DAYS}d" --include 'app-*.db.gz' --quiet \
        || log "remote pruning failed (ignored)"
    log "copied to remote"
fi

ping_monitor ""
log "done"
