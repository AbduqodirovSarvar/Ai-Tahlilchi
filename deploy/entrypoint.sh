#!/bin/sh
# Container entrypoint: migrate the database, then run the application as a supervised child.
#
# Why a wrapper instead of `exec python -m app.main`:
#  - migrations (`alembic upgrade head`) must finish before the application opens the
#    database (ADR 0007 section 12, ADR 0008); a failed migration stops the start, the old
#    schema stays untouched and the healthcheck/rollback path takes over;
#  - exit code 2 means a configuration error: restarting at once cannot fix it, and with
#    `restart: unless-stopped` Docker would spin on it forever. The wrapper waits
#    CONFIG_ERROR_BACKOFF_SECONDS before exiting 2, so the loop is slow (and the reason is
#    in the logs) instead of hot. Fix .env and run `docker compose up -d`.
#  - SIGTERM is forwarded to the application, which stops polling and waits for running
#    scheduler jobs (up to 60 s) before it exits; the wrapper then exits with its status.
#
# Usage: entrypoint.sh [command ...]   (default: python -m app.main)
set -eu

ALEMBIC_INI="${ALEMBIC_INI:-/app/alembic.ini}"
BACKOFF="${CONFIG_ERROR_BACKOFF_SECONDS:-60}"
EXIT_CONFIGURATION_ERROR=2

log() {
    # $1 level, $2 event, rest key=value pairs without quotes or spaces in values.
    level="$1"
    event="$2"
    shift 2
    printf '{"level": "%s", "event": "%s", "logger": "entrypoint", "detail": "%s"}\n' \
        "$level" "$event" "$*"
}

if [ "$#" -eq 0 ]; then
    set -- python -m app.main
fi

child=""
sleeper=""
stopping=0
reaped=0
reaped_status=0

# Runs after the interrupted `wait` returns: pass the signal on, then collect the child's real
# exit status here (a second `wait` in the main flow would race with the child's exit).
# shellcheck disable=SC2329  # invoked through the TERM/INT trap
on_signal() {
    stopping=1
    if [ -n "$sleeper" ]; then
        kill "$sleeper" 2>/dev/null || true
    fi
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        reaped_status=0
        wait "$child" || reaped_status=$?
        reaped=1
    fi
}
trap on_signal TERM INT

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    log info migrations_starting "alembic upgrade head"
    if ! alembic -c "$ALEMBIC_INI" upgrade head; then
        log critical migrations_failed "alembic upgrade head failed; the application was not started"
        exit 1
    fi
    log info migrations_done "database is at head"
fi

"$@" &
child=$!

status=0
wait "$child" || status=$?
if [ "$reaped" -eq 1 ]; then
    status="$reaped_status"
fi
child=""

if [ "$status" -eq "$EXIT_CONFIGURATION_ERROR" ] && [ "$stopping" -eq 0 ]; then
    log critical config_error_backoff "configuration error (exit 2); waiting ${BACKOFF}s before exit to avoid a restart loop"
    # The healthcheck reports this marker, so the container shows as unhealthy while it waits.
    marker="${TMPDIR:-/tmp}/ai-tahlilchi-config-error"
    : > "$marker"
    sleep "$BACKOFF" &
    sleeper=$!
    wait "$sleeper" || true
    sleeper=""
    rm -f "$marker"
fi

exit "$status"
