# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_MAJOR=20

# System packages:
#   git       — ingest worker commits to the wiki repo
#   curl, ca-certificates — Node.js install
#   build-essential — fallback for any wheel without prebuilt binaries
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# claude-agent-sdk shells out to the Claude Code CLI binary
RUN npm install -g @anthropic-ai/claude-code@latest

# Git defaults so the ingest worker can commit inside any mounted wiki repo
RUN git config --system user.email "llm-kb@localhost" \
    && git config --system user.name "llm-kb" \
    && git config --system --add safe.directory '*'

WORKDIR /app

# Install Python deps first (better layer caching)
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Source
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# In-container default paths — override via env in compose
ENV STATE_DB=/data/state.db \
    KB_ROOT=/wiki

# Default: print component help. docker-compose overrides per service.
CMD ["python", "-m", "app"]