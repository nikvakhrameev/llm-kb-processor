# Deployment

## Target environment

- VPS with Ubuntu 24.04 or Debian 12.
- 2 vCPU, 2 GB RAM minimum (4 GB recommended for comfortable Whisper
  transcription).
- 20 GB disk (knowledge base + git history + Docker images).
- Outbound internet to: `api.deepseek.com`, `api.anthropic.com`,
  `api.telegram.org`, `github.com` (for git push).

No inbound ports required (long-polling for Telegram).

## Filesystem layout on host

```
/opt/kb-bot/
├── app/                    # Python source code (this project)
├── knowledge_base/         # the wiki, separate git repo
├── state.db                # SQLite, persisted on host
├── .env                    # secrets (chmod 600)
├── docker-compose.yml
└── logs/                   # rotated worker logs
```

The application code (`app/`) is also in git but in a different repo from
the wiki, kept on the same host for simplicity.

## docker-compose

```yaml
# docker-compose.yml
networks:
  kb-egress-only:
    driver: bridge
    # see "Network restriction" below for iptables / hosts setup

services:
  bot:
    build: ./app
    image: kb-bot/app
    command: python -m app.bot
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./knowledge_base:/kb
      - ./state.db:/state.db
      - ./logs:/var/log/app
    networks:
      - default        # needs Telegram API access
    user: "1000:1000"

  resource-worker:
    image: kb-bot/app
    command: python -m app.worker_resources
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./knowledge_base:/kb
      - ./state.db:/state.db
      - ./logs:/var/log/app
    networks:
      - default        # needs DeepSeek + URL fetching
    user: "1000:1000"
    depends_on:
      - bot   # not strictly required, but starts together

  ingest-worker:
    image: kb-bot/app
    command: python -m app.worker_ingest
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./knowledge_base:/workspace
      # parsed sources read-only inside container
      - ./knowledge_base/raw/parsed:/workspace/raw/parsed:ro
      - ./state.db:/state.db
      - ./logs:/var/log/app
    networks:
      - kb-egress-only   # Anthropic API only
    read_only: true
    tmpfs:
      - /tmp:size=128m
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    user: "1000:1000"
    working_dir: /workspace

  scheduler:
    image: kb-bot/app
    command: python -m app.scheduler
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./knowledge_base:/kb
      - ./state.db:/state.db
      - ./logs:/var/log/app
    networks:
      - default
    user: "1000:1000"
```

The same `kb-bot/app` image is used by all four services with different
entrypoints. This keeps the build simple and ensures library versions stay
in lockstep.

## Ingest container restrictions explained

| Setting                       | Why                                                                  |
|-------------------------------|----------------------------------------------------------------------|
| `read_only: true`             | Root filesystem is RO. Agent writes only to mounted volumes + tmpfs. |
| `cap_drop: [ALL]`             | No Linux capabilities. Cannot mount, ptrace, change time, etc.       |
| `no-new-privileges`           | setuid binaries cannot escalate.                                     |
| `kb-egress-only` network      | Outbound traffic restricted to `api.anthropic.com:443`.              |
| `raw/parsed:/workspace/.../ro` | Belt-and-suspenders: source files cannot be tampered with.           |
| `user: 1000:1000`             | Non-root inside container.                                           |
| `tmpfs /tmp`                  | Some tools need scratch space; tmpfs is wiped on container restart.  |

## Network restriction for ingest

The simplest reliable approach on a single-host docker-compose is a
sidecar HTTP proxy that allows only `api.anthropic.com`:

```yaml
  egress-proxy:
    image: tinyproxy/tinyproxy
    networks:
      - kb-egress-only
      - default
    volumes:
      - ./docker/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:ro
```

`tinyproxy.conf`:
```
Port 8888
Listen 0.0.0.0
Allow 0.0.0.0/0     # within the docker network only
ConnectPort 443
ReversePath "/" "https://api.anthropic.com/"
Filter /etc/tinyproxy/filter
FilterURLs On
```

`docker/tinyproxy_filter`:
```
^https?://api\.anthropic\.com(/.*)?$
```

Then in `ingest-worker`, route Anthropic SDK through the proxy:

```yaml
    environment:
      - HTTPS_PROXY=http://egress-proxy:8888
      - HTTP_PROXY=http://egress-proxy:8888
```

This is one of several valid approaches. Alternatives: iptables rules on the
host scoped to the `kb-egress-only` bridge interface, or running the
ingest-worker on a Docker network with no default gateway and using a
`network_mode` extension to expose only the proxy. Pick one and document
the choice.

## Environment variables

`.env` template:

```
# --- Telegram ---
TELEGRAM_BOT_TOKEN=...
ALLOWED_USER_IDS=123456789,987654321
OWNER_CHAT_ID=123456789

# --- DeepSeek ---
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_GATE_MODEL=deepseek-v4-flash

# --- Anthropic ---
ANTHROPIC_API_KEY=...
INGEST_MODEL=claude-sonnet-4-7
SYNTHESIS_MODEL=claude-opus-4-7

# --- Paths ---
KB_ROOT=/kb
INGEST_KB_ROOT=/workspace
STATE_DB=/state.db

# --- Tuning ---
POLL_INTERVAL_SECONDS=2
GATE_ACCEPT_THRESHOLD=60
INGEST_TIMEOUT_SECONDS=600
INGEST_MAX_TURNS=25
LINT_MAX_TURNS=40
SYNTHESIS_MAX_TURNS=60
RETRIES_MAX=3
RETRY_BACKOFF_BASE_SECONDS=60
SWEEPER_STUCK_MINUTES=30

# --- Cron ---
LINT_HOUR=2
LINT_MINUTE=0
SYNTHESIS_DAY=sun
SYNTHESIS_HOUR=9
SYNTHESIS_MINUTE=0
TZ=Europe/Berlin

# --- Git auto-push ---
KB_GIT_REMOTE=git@github.com:youruser/yourkb.git
KB_GIT_AUTOPUSH=true
```

## Auto-push from host

The ingest container has no network access to GitHub. Pushing happens from
the host via a post-commit hook on the wiki repo:

```bash
# knowledge_base/.git/hooks/post-commit
#!/usr/bin/env bash
if [ "$KB_GIT_AUTOPUSH" = "true" ]; then
    git push origin main >> /var/log/app/git-push.log 2>&1 &
fi
```

Alternatively, a cron job on the host runs every 5 minutes and does
`cd knowledge_base && git push origin main` if there are unpushed commits.
Either works; the cron approach is simpler and survives the case where
multiple commits land before push completes.

## Initial setup steps

```bash
# 1. Clone application code
git clone git@github.com:youruser/kb-bot.git /opt/kb-bot
cd /opt/kb-bot

# 2. Initialize the wiki (separate repo)
mkdir knowledge_base
cd knowledge_base
git init
# create CLAUDE.md, purpose.md, index.md, log.md, raw/, wiki/ directory tree
git add -A && git commit -m "manual: initial wiki structure"
git remote add origin git@github.com:youruser/yourkb.git
git push -u origin main
cd ..

# 3. Initialize SQLite
sqlite3 state.db < app/migrations/0001_init.sql

# 4. Configure secrets
cp .env.example .env
chmod 600 .env
$EDITOR .env

# 5. Build and start
docker compose build
docker compose up -d

# 6. Verify
docker compose logs -f
```

Send a test message to the bot. Watch the resources table:

```bash
sqlite3 state.db "SELECT id, resource_type, status, error_message FROM resources ORDER BY created_at DESC LIMIT 10;"
```

## Backups

Two things to back up:

1. **`state.db`** — daily snapshot via `sqlite3 state.db ".backup
   /opt/kb-bot/backups/state-$(date +%F).db"` cron at 03:00. Keep 30 days.
2. **Wiki repo** — already backed up by the GitHub push. As an additional
   safety net, the daily lint also serves as a "wiki is alive" canary —
   if no `lint:` commit appears for 2 consecutive days, the scheduler
   sends a Telegram alert.

`raw/inbox/` and `raw/parsed/` are large but reproducible from the original
Telegram messages and parsing pipeline; they are not backed up in MVP.

## Logs

Each service writes to `logs/<service>.log` mounted from the host. Use
`logrotate` or `docker compose logs --rotate` to keep size bounded. INFO
level by default; DEBUG via `LOG_LEVEL` env var per service.

## Health checks

Lightweight check endpoint not needed — the bot is its own liveness
indicator (it replies). For workers, a watchdog cron on the host:

```bash
# /etc/cron.d/kb-bot-watchdog (every 10 minutes)
*/10 * * * * root /opt/kb-bot/scripts/watchdog.sh >> /var/log/kb-watchdog.log 2>&1
```

`watchdog.sh`:
- `docker compose ps` — alert if any service is not "running".
- `sqlite3 state.db "SELECT count(*) FROM resources WHERE status IN ('parsing','gating','ingesting') AND datetime(updated_at) < datetime('now','-1 hour')"` — alert if anything is stuck (the sweeper should have caught it, so this means the sweeper is stuck too).
