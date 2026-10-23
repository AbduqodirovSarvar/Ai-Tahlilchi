#!/usr/bin/env bash
# Restore the database from a backup made by backup.sh.
#
#   scripts/ops/restore.sh [--dry-run] [--yes] BACKUP_FILE.db.gz
#
# Steps: verify the backup (integrity + Alembic revision) -> ask for confirmation -> stop the
# bot (a running process would keep writing to the old file) -> restore, keeping the current
# database as a safety copy in <volume>/pre-restore/ -> start the bot again.
# A backup that does not verify is refused before anything is touched.
#
# After restoring, the container's entrypoint runs `alembic upgrade head`, so a backup taken
# on an older schema is migrated forward on the next start.
#
# Environment (all optional): APP_DIR, COMPOSE, TOOL_CMD, DB_PATH as in backup.sh, plus
#   STOP_CMD / START_CMD   override how the bot is stopped/started (tests use `true`).
#
# Exit codes: 0 ok, 1 failed, 64 usage, 65 not confirmed.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-tahlilchi}"
DB_PATH="${DB_PATH:-/data/app.db}"
DRY_RUN=0
ASSUME_YES=0
BACKUP_FILE=""

log() { printf '%s restore: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; exit 64 ;;
        *) [ -z "$BACKUP_FILE" ] || { echo "only one backup file may be given" >&2; exit 64; }
           BACKUP_FILE="$1" ;;
    esac
    shift
done
[ -n "$BACKUP_FILE" ] || { echo "usage: restore.sh [--dry-run] [--yes] BACKUP_FILE.db.gz" >&2; exit 64; }
[ -f "$BACKUP_FILE" ] || { log "ERROR: $BACKUP_FILE does not exist"; exit 1; }

if [ -z "${COMPOSE:-}" ]; then
    COMPOSE="docker compose --project-directory $APP_DIR -f $APP_DIR/docker-compose.yml --env-file $APP_DIR/.env"
    [ -f "$APP_DIR/deploy.env" ] && COMPOSE="$COMPOSE --env-file $APP_DIR/deploy.env"
fi
TOOL_CMD="${TOOL_CMD:-$COMPOSE run --rm --no-deps -T --entrypoint python bot -m app.ops.sqlite_tool}"
STOP_CMD="${STOP_CMD:-$COMPOSE stop bot}"
START_CMD="${START_CMD:-$COMPOSE up -d --no-build bot}"

# shellcheck disable=SC2086  # TOOL_CMD is a command line made of several words on purpose
if ! $TOOL_CMD verify --work-dir "$(dirname "$DB_PATH")" --input - < "$BACKUP_FILE"; then
    log "ERROR: $BACKUP_FILE does not verify; nothing was changed"
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] would stop the bot: $STOP_CMD"
    log "[dry-run] would restore $BACKUP_FILE into $DB_PATH (safety copy of the current database is kept)"
    log "[dry-run] would start the bot: $START_CMD"
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    printf 'This REPLACES the live database %s with %s.\nType RESTORE to continue: ' "$DB_PATH" "$BACKUP_FILE" >&2
    read -r answer || answer=""
    answer="${answer%$'\r'}" # tolerate CRLF input (Windows terminals, piped input)
    if [ "$answer" != "RESTORE" ]; then
        log "not confirmed; nothing was changed"
        exit 65
    fi
fi

log "stopping the bot"
$STOP_CMD

restored=0
# shellcheck disable=SC2086
if $TOOL_CMD restore --db "$DB_PATH" --input - < "$BACKUP_FILE"; then
    restored=1
else
    log "ERROR: restore failed; the previous database was left in place"
fi

log "starting the bot"
$START_CMD

[ "$restored" -eq 1 ] || exit 1
log "done; check 'docker compose logs bot' and the /holat command"
