#!/usr/bin/env bash
# One-time (idempotent) preparation of a fresh Ubuntu 24.04 VPS for AI Tahlilchi.
#
#   sudo ./bootstrap.sh --ssh-key-file ~/deploy_key.pub [--dry-run] [options]
#
# What it does (each step is safe to repeat):
#   1. packages: ufw, fail2ban, unattended-upgrades, curl, gnupg, sqlite3 (diagnostics)
#   2. non-root deploy user with the given SSH public key (the only way to log in)
#   3. sshd hardening drop-in: key-only, no root login, only the deploy user
#      (validated with `sshd -t` before reload; keep your current session open and test a
#      second login before closing it)
#   4. ufw: deny incoming except SSH (rate limited); fail2ban for sshd
#   5. unattended security upgrades
#   6. Docker Engine + Compose plugin from Docker's apt repository, log rotation defaults
#   7. /opt/ai-tahlilchi (+ backups, bin), an EMPTY .env with chmod 600 (you fill it in)
#   8. a 1 GB swap file on small machines (<= 2 GB RAM) so a memory spike does not kill the bot
#   9. systemd units: daily backup timer (always), optional boot service (--install-service)
#
# Sizing: 2 vCPU / 2 GB RAM / 20+ GB disk is comfortable (the bot idles near 200 MB, SQLite
# stays under 1 GB for years); 1 vCPU / 1 GB works with the swap file. Hetzner CX22 or any
# equivalent, in Europe for low latency to the APIs.
#
# Options:
#   --ssh-key-file FILE   public key to install for the deploy user (required)
#   --deploy-user NAME    default: deploy
#   --ssh-port PORT       default: 22; the port sshd ALREADY listens on (ufw allows exactly it)
#   --app-dir DIR         default: /opt/ai-tahlilchi
#   --no-swap             do not create a swap file
#   --install-service     also enable deploy/ai-tahlilchi.service (alternative to Docker's
#                         own `restart: unless-stopped`, see ADR 0014)
#   --dry-run             print what would be done, change nothing (works without root)
#   -h, --help
set -euo pipefail

DEPLOY_USER="deploy"
SSH_PORT="22"
APP_DIR="/opt/ai-tahlilchi"
SSH_KEY_FILE=""
WANT_SWAP=1
INSTALL_SERVICE=0
DRY_RUN=0
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# Run a command, or only show it in dry-run mode.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

# Write stdin to a file (mode $2) unless it already has exactly that content.
put_file() {
    local path="$1" mode="$2" content
    content="$(cat)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] write %s (mode %s):\n' "$path" "$mode"
        printf '%s\n' "$content" | sed 's/^/    | /'
        return 0
    fi
    if [ -f "$path" ] && [ "$(cat "$path")" = "$content" ]; then
        log "unchanged: $path"
        return 0
    fi
    printf '%s\n' "$content" > "$path"
    chmod "$mode" "$path"
    log "wrote $path"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --ssh-key-file) shift; SSH_KEY_FILE="${1:?--ssh-key-file needs a value}" ;;
        --deploy-user) shift; DEPLOY_USER="${1:?--deploy-user needs a value}" ;;
        --ssh-port) shift; SSH_PORT="${1:?--ssh-port needs a value}" ;;
        --app-dir) shift; APP_DIR="${1:?--app-dir needs a value}" ;;
        --no-swap) WANT_SWAP=0 ;;
        --install-service) INSTALL_SERVICE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
    shift
done

case "$DEPLOY_USER" in ''|*[!a-z0-9_-]*|-*) die "invalid --deploy-user" ;; esac
case "$SSH_PORT" in ''|*[!0-9]*) die "invalid --ssh-port" ;; esac
case "$APP_DIR" in /*) ;; *) die "--app-dir must be an absolute path" ;; esac
[ -n "$SSH_KEY_FILE" ] || die "--ssh-key-file is required: without a key the password login cannot be disabled safely"
[ "$DRY_RUN" -eq 1 ] || [ -f "$SSH_KEY_FILE" ] || die "$SSH_KEY_FILE does not exist"

if [ "$DRY_RUN" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || die "run as root (sudo); use --dry-run to preview"
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
            log "WARNING: tested on Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}"
        fi
    fi
    ssh-keygen -l -f "$SSH_KEY_FILE" >/dev/null 2>&1 || die "$SSH_KEY_FILE is not a valid SSH public key"
fi

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- 1. packages
log "1/9 packages"
run apt-get update -qq
run apt-get install -y -qq ufw fail2ban unattended-upgrades ca-certificates curl gnupg sqlite3

# ---------------------------------------------------------------- 2. deploy user
log "2/9 deploy user '$DEPLOY_USER'"
if [ "$DRY_RUN" -eq 1 ] || ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    run adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] install %s into ~%s/.ssh/authorized_keys (idempotent)\n' "$SSH_KEY_FILE" "$DEPLOY_USER"
else
    home="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
    install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
    touch "$home/.ssh/authorized_keys"
    while IFS= read -r key_line; do
        [ -n "$key_line" ] || continue
        grep -qxF "$key_line" "$home/.ssh/authorized_keys" || printf '%s\n' "$key_line" >> "$home/.ssh/authorized_keys"
    done < "$SSH_KEY_FILE"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"
    chmod 600 "$home/.ssh/authorized_keys"
fi

# ---------------------------------------------------------------- 3. sshd
log "3/9 sshd hardening"
put_file /etc/ssh/sshd_config.d/99-ai-tahlilchi.conf 644 <<EOF
# Managed by deploy/bootstrap.sh
# (the port is not set here: Ubuntu 24.04 starts sshd through ssh.socket, which owns it)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers $DEPLOY_USER
MaxAuthTries 3
X11Forwarding no
EOF
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] sshd -t && systemctl reload ssh"
else
    sshd -t || die "sshd configuration is invalid; the drop-in was written but ssh was NOT reloaded"
    systemctl reload ssh
fi

# ---------------------------------------------------------------- 4. firewall + fail2ban
log "4/9 ufw and fail2ban"
run ufw default deny incoming
run ufw default allow outgoing
run ufw limit "$SSH_PORT/tcp"
run ufw --force enable
put_file /etc/fail2ban/jail.d/ai-tahlilchi.local 644 <<EOF
[sshd]
enabled = true
port = $SSH_PORT
maxretry = 4
findtime = 10m
bantime = 1h
EOF
run systemctl enable --now fail2ban
run systemctl restart fail2ban

# ---------------------------------------------------------------- 5. unattended upgrades
log "5/9 unattended security upgrades"
put_file /etc/apt/apt.conf.d/20auto-upgrades 644 <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
run systemctl enable --now unattended-upgrades

# ---------------------------------------------------------------- 6. docker
log "6/9 Docker Engine"
if [ "$DRY_RUN" -eq 1 ] || ! command -v docker >/dev/null 2>&1; then
    run install -m 0755 -d /etc/apt/keyrings
    run curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    run chmod a+r /etc/apt/keyrings/docker.asc
    put_file /etc/apt/sources.list.d/docker.list 644 <<EOF
deb [arch=$(dpkg --print-architecture 2>/dev/null || echo amd64) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable
EOF
    run apt-get update -qq
    run apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
else
    log "docker already installed"
fi
run install -d -m 755 /etc/docker
if [ "$DRY_RUN" -eq 1 ] || [ ! -f /etc/docker/daemon.json ]; then
    put_file /etc/docker/daemon.json 644 <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true
}
EOF
    run systemctl restart docker
fi
run systemctl enable --now docker
run usermod -aG docker "$DEPLOY_USER"

# ---------------------------------------------------------------- 7. application directory
log "7/9 $APP_DIR"
run install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$APP_DIR/backups" "$APP_DIR/bin"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] create empty $APP_DIR/.env (chmod 600, owner $DEPLOY_USER) if missing"
elif [ ! -f "$APP_DIR/.env" ]; then
    install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null "$APP_DIR/.env"
    cat > "$APP_DIR/.env" <<'EOF'
# Fill in and keep chmod 600. Reference: .env.example in the repository, RUNBOOK.md.
TELEGRAM_BOT_TOKEN=
ADMIN_IDS=
# Switch to real once all keys are present:
APP_SERVICES=fake
API_FOOTBALL_KEY=
GEMINI_API_KEY=
ODDS_API_KEY=
HEALTHCHECKS_URL=
EOF
    log "created $APP_DIR/.env (empty): fill it in as the $DEPLOY_USER user"
else
    chmod 600 "$APP_DIR/.env"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"
    log ".env exists: permissions enforced, content untouched"
fi

# ---------------------------------------------------------------- 8. swap
log "8/9 swap"
mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$WANT_SWAP" -eq 1 ] && [ "$mem_kb" -le 2200000 ] && [ "$DRY_RUN" -eq 0 ]; then
    if [ -z "$(swapon --show --noheadings)" ]; then
        fallocate -l 1G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        log "created 1 GB swap"
    else
        log "swap already present"
    fi
else
    log "swap skipped (--no-swap, enough RAM or dry-run)"
fi

# ---------------------------------------------------------------- 9. systemd
log "9/9 systemd units"
for unit in ai-tahlilchi-backup.service ai-tahlilchi-backup.timer ai-tahlilchi.service; do
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] install %s/%s -> /etc/systemd/system/%s (APP_DIR/user substituted)\n' "$SOURCE_DIR" "$unit" "$unit"
    else
        sed -e "s#/opt/ai-tahlilchi#$APP_DIR#g" -e "s#^User=deploy#User=$DEPLOY_USER#" \
            "$SOURCE_DIR/$unit" > "/etc/systemd/system/$unit"
    fi
done
run systemctl daemon-reload
run systemctl enable --now ai-tahlilchi-backup.timer
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    run systemctl enable ai-tahlilchi.service
fi

log "done. Next: log in as $DEPLOY_USER in a NEW terminal to confirm key access, then follow RUNBOOK.md (first deploy)."
