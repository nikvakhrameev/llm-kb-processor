#!/usr/bin/env bash
# One-time VPS bootstrap for llm-kb deployment.
#
# Runs locally and SSHes into the VPS to:
#   1. verify SSH connectivity
#   2. install Docker engine + compose plugin if missing
#   3. create the deploy directory the CI/CD pipeline scps docker-compose.yml into,
#      seeding an .env stub and a `data/` subdir for the SQLite state file
#   4. optionally scp the local state.db (+ wal/shm siblings) into <path>/data/
#
# Requirements on the VPS: Debian/Ubuntu, and either root SSH or passwordless sudo.
#
# Usage:
#   scripts/vps-init.sh \
#       --host <vps-host> \
#       --user <ssh-user> \
#       --path <remote-deploy-path> \
#       [--port <ssh-port>] \
#       [--key <ssh-private-key>] \
#       [--app-user <name>] \
#       [--copy-db [<local-state.db>]]
#
# --app-user adds an existing non-SSH user (e.g. `deploy`) to the docker group.
# Useful when you log in as root only to bootstrap and a separate account will
# run the app. The user must already exist on the VPS; this only flips groups.
#
# Examples:
#   scripts/vps-init.sh --host vps.example.com --user deploy --path /home/deploy/llm-kb
#   scripts/vps-init.sh --host 1.2.3.4 --user root --path /opt/llm-kb --copy-db
#   scripts/vps-init.sh --host h --user root --path /home/deploy/llm-kb --app-user deploy
#   scripts/vps-init.sh --host h --user u --path /opt/llm-kb --copy-db ./state.db --key ~/.ssh/id_ed25519

set -euo pipefail

HOST=""
SSH_USER=""
PORT=22
KEY=""
DEPLOY_PATH=""
APP_USER=""
COPY_DB=0
LOCAL_DB="./state.db"

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --key)  KEY="$2"; shift 2 ;;
        --path) DEPLOY_PATH="$2"; shift 2 ;;
        --app-user) APP_USER="$2"; shift 2 ;;
        --copy-db)
            COPY_DB=1
            # Optional positional: explicit local state.db path
            if [[ ${2:-} && ${2:0:2} != "--" ]]; then
                LOCAL_DB="$2"
                shift
            fi
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$HOST" || -z "$SSH_USER" || -z "$DEPLOY_PATH" ]]; then
    echo "error: --host, --user, --path are required" >&2
    usage >&2
    exit 2
fi

SSH_OPTS=(-p "$PORT" -o StrictHostKeyChecking=accept-new)
SCP_OPTS=(-P "$PORT" -o StrictHostKeyChecking=accept-new)
if [[ -n "$KEY" ]]; then
    SSH_OPTS+=(-i "$KEY")
    SCP_OPTS+=(-i "$KEY")
fi
REMOTE="${SSH_USER}@${HOST}"

# ------------------------------------------------------------------
# 1. Connectivity check
# ------------------------------------------------------------------
echo "[1/4] testing SSH to ${REMOTE} ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" 'echo "  connected: $(hostname) ($(uname -sr))"'

# ------------------------------------------------------------------
# 2. Install Docker + compose plugin if missing
# ------------------------------------------------------------------
echo "[2/4] ensuring Docker engine + compose plugin on ${HOST} ..."
# Allocate a TTY only when the SSH user is non-root (sudo may prompt for a password).
# When logging in as root, -tt leaves the remote shell hanging in interactive mode
# after the heredoc closes, so plain ssh is used instead.
SSH_DOCKER_OPTS=("${SSH_OPTS[@]}")
[[ "$SSH_USER" != "root" ]] && SSH_DOCKER_OPTS=(-tt "${SSH_DOCKER_OPTS[@]}")
ssh "${SSH_DOCKER_OPTS[@]}" "$REMOTE" "bash -s -- '${APP_USER}'" <<'REMOTE_EOF'
set -euo pipefail

APP_USER="${1:-}"

SUDO=""
[[ "$(id -u)" -ne 0 ]] && SUDO="sudo"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "  docker present: $(docker --version)"
    echo "  compose plugin present: $(docker compose version)"
else
    echo "  installing via get.docker.com ..."
    curl -fsSL https://get.docker.com | $SUDO sh
fi

add_to_docker_group() {
    local u="$1"
    if ! id "$u" >/dev/null 2>&1; then
        echo "  warning: user '$u' does not exist on the VPS — skipping docker-group add" >&2
        return
    fi
    if id -nG "$u" | grep -qw docker; then
        echo "  $u already in docker group"
    else
        $SUDO usermod -aG docker "$u"
        echo "  added $u to docker group (new SSH session as $u needed for groups to take effect)"
    fi
}

me="$(whoami)"
[[ "$me" != "root" ]] && add_to_docker_group "$me"
[[ -n "$APP_USER" && "$APP_USER" != "$me" ]] && add_to_docker_group "$APP_USER"

$SUDO systemctl enable --now docker >/dev/null
echo "  docker service: $($SUDO systemctl is-active docker)"
REMOTE_EOF

# ------------------------------------------------------------------
# 3. Prepare deploy directory
# ------------------------------------------------------------------
echo "[3/4] preparing ${DEPLOY_PATH} on ${HOST} ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" bash -s <<REMOTE_EOF
set -euo pipefail

SUDO=""
[[ "\$(id -u)" -ne 0 ]] && SUDO="sudo"

# Target ownership: --app-user when set, otherwise the SSH login user.
target_user="${APP_USER:-\$(whoami)}"
if ! id "\$target_user" >/dev/null 2>&1; then
    echo "  warning: target user '\$target_user' does not exist — falling back to \$(whoami)" >&2
    target_user="\$(whoami)"
fi

\$SUDO mkdir -p "${DEPLOY_PATH}/data"

if [[ ! -f "${DEPLOY_PATH}/.env" ]]; then
    \$SUDO tee "${DEPLOY_PATH}/.env" >/dev/null <<'ENV_STUB'
# Populate with secrets and runtime config — see .env.example in the repo.
# IMAGE is injected by the CI/CD deploy step; do not hardcode it here.
STATE_DB_DIR=./data
WIKI_DIR=./llm-kb-wiki

DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat

ANTHROPIC_BASE_URL=https://api.deepseek.com/v1
ANTHROPIC_AUTH_TOKEN=

TELEGRAM_BOT_TOKEN=
ALLOWED_USER_IDS=
OWNER_CHAT_ID=
ENV_STUB
    echo "  created ${DEPLOY_PATH}/.env stub — fill in secrets before first deploy"
else
    echo "  ${DEPLOY_PATH}/.env already exists, left as-is"
fi

\$SUDO chown -R "\$target_user:\$target_user" "${DEPLOY_PATH}"
echo "  ${DEPLOY_PATH} owned by \$target_user"

echo "  layout:"
ls -la "${DEPLOY_PATH}"
REMOTE_EOF

# ------------------------------------------------------------------
# 4. Optional: seed state.db
# ------------------------------------------------------------------
if [[ "$COPY_DB" -eq 1 ]]; then
    echo "[4/4] copying ${LOCAL_DB} (+ wal/shm if present) to ${REMOTE}:${DEPLOY_PATH}/data/ ..."
    if [[ ! -f "$LOCAL_DB" ]]; then
        echo "  error: local state.db not found at ${LOCAL_DB}" >&2
        exit 3
    fi
    files=("$LOCAL_DB")
    [[ -f "${LOCAL_DB}-wal" ]] && files+=("${LOCAL_DB}-wal")
    [[ -f "${LOCAL_DB}-shm" ]] && files+=("${LOCAL_DB}-shm")
    scp "${SCP_OPTS[@]}" "${files[@]}" "${REMOTE}:${DEPLOY_PATH}/data/"
    echo "  copied: ${files[*]##*/}"

    # scp landed as the SSH user — re-chown so the app user can read/write.
    ssh "${SSH_OPTS[@]}" "$REMOTE" bash -s <<REMOTE_EOF
set -euo pipefail
SUDO=""
[[ "\$(id -u)" -ne 0 ]] && SUDO="sudo"
target_user="${APP_USER:-\$(whoami)}"
id "\$target_user" >/dev/null 2>&1 || target_user="\$(whoami)"
\$SUDO chown -R "\$target_user:\$target_user" "${DEPLOY_PATH}/data"
REMOTE_EOF
else
    echo "[4/4] --copy-db not set, skipping state.db seed"
fi

if [[ -n "$APP_USER" ]]; then
    cicd_user="$APP_USER"
    note_app_user="
  Note: ${APP_USER} was added to the docker group. Open a fresh SSH session as
  ${APP_USER} (with its own SSH key) before any docker commands — group changes
  take effect on next login."
else
    cicd_user="$SSH_USER"
    note_app_user=""
fi

cat <<DONE

done.${note_app_user}

Next steps:
  1. ssh ${REMOTE} and edit ${DEPLOY_PATH}/.env with real API keys / Telegram tokens.
  2. Clone the wiki repo on the VPS:
       git clone <wiki-remote> ${DEPLOY_PATH}/llm-kb-wiki
     (or run scripts/init-wiki.sh ${DEPLOY_PATH}/llm-kb-wiki for a fresh one).
  3. Configure GitHub repo secrets so the CI/CD deploy job can connect:
       VPS_HOST=${HOST}
       VPS_USER=${cicd_user}
       VPS_SSH_KEY=<contents of the matching private key>
       VPS_SSH_PORT=${PORT}            # optional, defaults to 22
       VPS_DEPLOY_PATH=${DEPLOY_PATH}
       GHCR_USER=<github username>
       GHCR_TOKEN=<PAT with read:packages>
  4. Push to main/master/develop to trigger the first deploy.
DONE
