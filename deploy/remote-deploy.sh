#!/usr/bin/env bash
# Server-side deploy: back up, pull the new image, switch, wait for "healthy", roll back if not.
# Run by .github/workflows/deploy.yml over SSH; also usable by hand (RUNBOOK.md).
#
#   deploy/remote-deploy.sh [--dry-run] [--skip-backup] IMAGE_REF
#
# IMAGE_REF is a full image reference, e.g. ghcr.io/owner/ai-tahlilchi:v1.2.3. Rolling back by
# hand is the same command with the previous reference (kept in deploy.env.prev).
#
# Safety:
#  1. pre-deploy database backup (a failed backup aborts BEFORE anything is changed);
#  2. `compose pull` first: a missing image or registry outage changes nothing;
#  3. the new reference is written to deploy.env only after the pull succeeded;
#  4. after `up -d` the container must report healthy within HEALTH_TIMEOUT_MINUTES; the
#     image healthcheck covers database, scheduler liveness and disk. Otherwise the previous
#     reference is restored and started again, and the script exits 1.
# Schema note: migrations run forward on start and are not undone by a rollback. Migrations
# are written to be backward compatible for one release; if the old image cannot run on the
# new schema, restore the pre-deploy backup (RUNBOOK.md).
#
# Registry login: when GHCR_USER is set, the token is read from the FIRST LINE OF STDIN (never
# from arguments or the environment, so it cannot show up in process listings or logs) and the
# session is removed again before the script ends.
#
# Environment: APP_DIR (/opt/ai-tahlilchi), HEALTH_TIMEOUT_MINUTES (5), POLL_SECONDS (5),
# DOCKER (docker), BACKUP_CMD (path of backup.sh).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-tahlilchi}"
HEALTH_TIMEOUT_MINUTES="${HEALTH_TIMEOUT_MINUTES:-5}"
POLL_SECONDS="${POLL_SECONDS:-5}"
DOCKER="${DOCKER:-docker}"
BACKUP_CMD="${BACKUP_CMD:-$APP_DIR/bin/backup.sh}"
REGISTRY="${REGISTRY:-ghcr.io}"
STABLE_POLLS=2
DRY_RUN=0
SKIP_BACKUP=0
IMAGE=""

log() { printf '%s deploy: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --skip-backup) SKIP_BACKUP=1 ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; exit 64 ;;
        *) [ -z "$IMAGE" ] || { echo "only one image reference may be given" >&2; exit 64; }
           IMAGE="$1" ;;
    esac
    shift
done
[ -n "$IMAGE" ] || { echo "usage: remote-deploy.sh [--dry-run] [--skip-backup] IMAGE_REF" >&2; exit 64; }
# Registry/repo[:tag] characters only; stops accidental shell metacharacters and option injection.
case "$IMAGE" in
    -*|*[!A-Za-z0-9._/:@-]*) echo "invalid image reference" >&2; exit 64 ;;
esac
case "$HEALTH_TIMEOUT_MINUTES" in ''|*[!0-9]*) echo "HEALTH_TIMEOUT_MINUTES must be a number" >&2; exit 64 ;; esac

cd "$APP_DIR"
DEPLOY_ENV="$APP_DIR/deploy.env"
compose() {
    local files=(--project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml")
    [ ! -f "$APP_DIR/.env" ] || files+=(--env-file "$APP_DIR/.env")
    files+=(--env-file "$DEPLOY_ENV")
    "$DOCKER" compose "${files[@]}" "$@"
}

current_image() {
    [ -f "$DEPLOY_ENV" ] || return 0
    sed -n 's/^AI_TAHLILCHI_IMAGE=//p' "$DEPLOY_ENV" | tail -n 1
}

write_image() {
    local tmp
    tmp="$(mktemp "$APP_DIR/.deploy.env.XXXXXX")"
    printf 'AI_TAHLILCHI_IMAGE=%s\n' "$1" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$DEPLOY_ENV"
}

wait_healthy() {
    # Returns 0 when the bot container is healthy for STABLE_POLLS consecutive polls.
    local polls=$(( HEALTH_TIMEOUT_MINUTES * 60 / (POLL_SECONDS > 0 ? POLL_SECONDS : 1) ))
    local stable=0 id health state
    local i
    for (( i = 0; i < polls; i++ )); do
        id="$(compose ps -q bot 2>/dev/null || true)"
        health=""
        state=""
        if [ -n "$id" ]; then
            health="$("$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id" 2>/dev/null || true)"
            state="$("$DOCKER" inspect -f '{{.State.Status}}' "$id" 2>/dev/null || true)"
        fi
        if [ "$health" = "healthy" ]; then
            stable=$(( stable + 1 ))
            [ "$stable" -ge "$STABLE_POLLS" ] && return 0
        else
            stable=0
        fi
        case "$state" in exited|dead) log "container state: $state"; return 1 ;; esac
        [ "$health" != "unhealthy" ] || { log "container is unhealthy"; return 1; }
        [ "$POLL_SECONDS" -le 0 ] || sleep "$POLL_SECONDS"
    done
    log "not healthy within ${HEALTH_TIMEOUT_MINUTES} minute(s) (last state: ${state:-none}/${health:-none})"
    return 1
}

previous="$(current_image)"
log "current image: ${previous:-none}; new image: $IMAGE"

if [ "$IMAGE" = "$previous" ]; then
    log "already deployed; re-applying to be safe"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    [ "$SKIP_BACKUP" -eq 1 ] || [ -z "$previous" ] || log "[dry-run] $BACKUP_CMD --label pre-deploy"
    log "[dry-run] login to $REGISTRY if GHCR_USER is set; compose pull; write deploy.env; compose up -d --no-build"
    log "[dry-run] wait up to ${HEALTH_TIMEOUT_MINUTES} min for healthy, else restore ${previous:-nothing}"
    exit 0
fi

logged_in=0
# shellcheck disable=SC2329  # invoked through the EXIT trap
cleanup() {
    if [ "$logged_in" -eq 1 ]; then "$DOCKER" logout "$REGISTRY" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if [ -n "${GHCR_USER:-}" ]; then
    IFS= read -r token || token=""
    [ -n "$token" ] || { log "ERROR: GHCR_USER is set but no token was given on stdin"; exit 1; }
    printf '%s\n' "$token" | "$DOCKER" login "$REGISTRY" -u "$GHCR_USER" --password-stdin >/dev/null
    unset token
    logged_in=1
fi

if [ "$SKIP_BACKUP" -eq 0 ] && [ -n "$previous" ]; then
    log "pre-deploy backup"
    "$BACKUP_CMD" --label pre-deploy || { log "ERROR: backup failed; nothing was changed"; exit 1; }
fi

# First deploy: compose needs a deploy.env to interpolate even for `pull`.
[ -f "$DEPLOY_ENV" ] || write_image "$IMAGE"
cp "$DEPLOY_ENV" "$DEPLOY_ENV.prev"
printf 'AI_TAHLILCHI_IMAGE=%s\n' "$IMAGE" > "$DEPLOY_ENV.new"
chmod 600 "$DEPLOY_ENV.new"

log "pulling $IMAGE"
if ! "$DOCKER" pull "$IMAGE"; then
    rm -f "$DEPLOY_ENV.new"
    [ -n "$previous" ] || rm -f "$DEPLOY_ENV" "$DEPLOY_ENV.prev"
    log "ERROR: pull failed; nothing was changed"
    exit 1
fi

mv "$DEPLOY_ENV.new" "$DEPLOY_ENV"
log "starting the new version"
compose up -d --no-build --remove-orphans bot

if wait_healthy; then
    log "healthy; deploy done"
    "$DOCKER" image prune -f >/dev/null 2>&1 || true
    exit 0
fi

log "ERROR: the new version did not become healthy"
if [ -z "$previous" ]; then
    log "no previous version to roll back to (first deploy); see: docker compose logs bot"
    exit 1
fi
log "rolling back to $previous"
write_image "$previous"
compose up -d --no-build --remove-orphans bot
if wait_healthy; then
    log "rolled back; the previous version is healthy again"
else
    log "ERROR: the previous version is not healthy either; investigate with docker compose logs bot (RUNBOOK.md)"
fi
exit 1
