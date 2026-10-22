#!/usr/bin/env bash
# `docker compose` with the server's project settings (installed as /opt/ai-tahlilchi/bin/dc).
#
#   bin/dc ps | logs -f bot | restart bot | exec bot python -m app.ops.healthcheck
#
# It adds the compose file, .env (secrets) and deploy.env (the image tag written by
# remote-deploy.sh) so that plain `docker compose` mistakes (wrong directory, missing image
# tag) cannot start the wrong version.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-tahlilchi}"
args=(--project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml")
[ ! -f "$APP_DIR/.env" ] || args+=(--env-file "$APP_DIR/.env")
[ ! -f "$APP_DIR/deploy.env" ] || args+=(--env-file "$APP_DIR/deploy.env")
exec docker compose "${args[@]}" "$@"
